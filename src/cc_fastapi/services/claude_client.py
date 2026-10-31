import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Callable, Literal

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from cc_fastapi.core.config import get_settings

logger = logging.getLogger(__name__)
MAX_CLI_STDERR_CHARS = 8_000
MAX_PARTIAL_OUTPUT_CHARS = 4_000
MAX_EXECUTION_ERROR_CHARS = 12_000
CONTROL_POLL_SECONDS = 0.25
INTERRUPT_GRACE_SECONDS = 5

PAUSE_RESUME_PROMPT = (
    "Continue the original unfinished task after a service pause. "
    "Use the existing conversation and preserve the original requirements. "
    "Check the outcome of any interrupted tool operation before repeating it. "
    "Finish the remaining work and return the required final result."
)

CONTINUE_RESUME_PROMPT = (
    "The previous run was marked finished, but it may have stopped before the work "
    "was actually complete. Treat the existing conversation as the source of truth. "
    "First verify what has already been done (files, commands, tool results, external "
    "side effects) and do not repeat completed side-effecting operations. Then finish "
    "the remaining work and return the final required result."
)

ResumeReason = Literal["pause", "continue"]


class ClaudeExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        exit_code: int | None,
        cli_stderr: str,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.exit_code = exit_code
        self.cli_stderr = cli_stderr


class AgentTaskCancelledError(RuntimeError):
    pass


class AgentTaskPausedError(RuntimeError):
    pass


class AgentTaskStopError(RuntimeError):
    """The SDK process has not confirmed shutdown; do not acknowledge pause."""


def validate_claude_agent_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Extension hook: currently pass-through without strict validation."""
    if options is None:
        return {}
    if not isinstance(options, dict):
        return {}
    cwd_value = options.get("cwd")
    if isinstance(cwd_value, (str, os.PathLike)):
        cwd_str = os.fspath(cwd_value).strip()
        if cwd_str and Path(cwd_str).is_absolute():
            raise ValueError("claude_agent_options.cwd must be a relative path")
    return options


def _normalize_tools(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _resolve_and_ensure_cwd(value: Any, fallback_cwd: str) -> str:
    raw_value: str
    if isinstance(value, (str, os.PathLike)):
        raw_value = os.fspath(value).strip()
    else:
        raw_value = ""
    if not raw_value:
        raw_value = fallback_cwd
    cwd_path = Path(raw_value)
    if not cwd_path.is_absolute():
        cwd_path = Path.cwd() / cwd_path
    created = not cwd_path.exists()
    cwd_path.mkdir(parents=True, exist_ok=True)
    logger.debug(
        "resolved claude cwd",
        extra={
            "event_type": "claude_cwd_resolved",
            "reason": "created" if created else "exists",
        },
    )
    return str(cwd_path)


def _redact_diagnostic_text(value: str, api_key: str) -> str:
    sanitized = value.strip()
    if api_key:
        sanitized = sanitized.replace(api_key, "[REDACTED_API_KEY]")
    return sanitized


def _bounded_stderr(lines: list[str]) -> str:
    combined = "\n".join(line for line in lines if line).strip()
    if len(combined) <= MAX_CLI_STDERR_CHARS:
        return combined
    return f"[earlier stderr truncated]\n{combined[-MAX_CLI_STDERR_CHARS:]}"


class ClaudeClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def run_agent_task(
        self,
        *,
        prompt: str,
        model: str,
        metadata: dict[str, Any] | None,
        claude_agent_options: dict[str, Any] | None = None,
        agent_mode: bool = True,
        unattended: bool = True,
        on_message_update: Callable[[list[str]], None] | None = None,
        on_session_id: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        resume_session_id: str | None = None,
        resume_reason: ResumeReason | None = None,
    ) -> dict[str, Any]:
        if not self.settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is missing")
        logger.debug("run_agent_task called", extra={"event_type": "claude_run_task_called"})
        return asyncio.run(
            self._run_agent_task_async(
                prompt=prompt,
                model=model,
                metadata=metadata,
                claude_agent_options=claude_agent_options,
                agent_mode=agent_mode,
                unattended=unattended,
                on_message_update=on_message_update,
                on_session_id=on_session_id,
                should_cancel=should_cancel,
                should_pause=should_pause,
                resume_session_id=resume_session_id,
                resume_reason=resume_reason,
            )
        )

    async def _run_agent_task_async(
        self,
        *,
        prompt: str,
        model: str,
        metadata: dict[str, Any] | None,
        claude_agent_options: dict[str, Any] | None,
        agent_mode: bool,
        unattended: bool,
        on_message_update: Callable[[list[str]], None] | None,
        on_session_id: Callable[[str], None] | None,
        should_cancel: Callable[[], bool] | None,
        should_pause: Callable[[], bool] | None,
        resume_session_id: str | None,
        resume_reason: ResumeReason | None,
    ) -> dict[str, Any]:
        system_note = (
            "You are running in Claude Agent task mode. "
            f"agent_mode={str(agent_mode).lower()}, unattended={str(unattended).lower()}. "
            f"metadata={metadata or {}}."
        )

        env = {
            "ANTHROPIC_API_KEY": self.settings.anthropic_api_key,
            "API_TIMEOUT_MS": str(self.settings.api_timeout_ms),
        }
        anthropic_base_url = self.settings.anthropic_base_url.strip().rstrip("/")
        if anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = anthropic_base_url
        if self.settings.anthropic_default_opus_model:
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = self.settings.anthropic_default_opus_model
        if self.settings.anthropic_default_sonnet_model:
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = self.settings.anthropic_default_sonnet_model
        if self.settings.anthropic_default_haiku_model:
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = self.settings.anthropic_default_haiku_model
        allowed_tools = _normalize_tools(self.settings.claude_allowed_tools)
        disallowed_tools = _normalize_tools(self.settings.claude_disallowed_tools)

        options_kwargs: dict[str, Any] = {
            "model": model,
            "permission_mode": self.settings.claude_permission_mode,
            "max_turns": self.settings.claude_max_turns,
            "cwd": os.path.abspath(self.settings.claude_cwd),
            "system_prompt": system_note,
            "env": env,
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
            "setting_sources": ["user", "project"],
        }
        user_options = validate_claude_agent_options(claude_agent_options)
        logger.debug(
            "claude options validated",
            extra={"event_type": "claude_options_validated", "reason": f"user_keys={sorted(user_options.keys())}"},
        )
        if "env" in user_options and isinstance(user_options["env"], dict):
            merged_env = {**env, **user_options["env"]}
            merged_env["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key
            user_options = {**user_options, "env": merged_env}
        options_kwargs.update(user_options)
        options_kwargs["allowed_tools"] = _normalize_tools(options_kwargs.get("allowed_tools"))
        options_kwargs["disallowed_tools"] = _normalize_tools(options_kwargs.get("disallowed_tools"))
        options_kwargs["cwd"] = _resolve_and_ensure_cwd(options_kwargs.get("cwd"), self.settings.claude_cwd)
        logger.debug(
            "claude options prepared",
            extra={
                "event_type": "claude_options_prepared",
                "reason": (
                    f"allowed={len(options_kwargs['allowed_tools'])},"
                    f"disallowed={len(options_kwargs['disallowed_tools'])},"
                    f"max_turns={options_kwargs.get('max_turns')}"
                ),
            },
        )

        stderr_lines: list[str] = []

        def capture_stderr(raw_line: str) -> None:
            line = _redact_diagnostic_text(raw_line, self.settings.anthropic_api_key)
            if line:
                stderr_lines.append(line)
                while len("\n".join(stderr_lines)) > MAX_CLI_STDERR_CHARS * 2:
                    stderr_lines.pop(0)

        # Always retain the internal diagnostic callback, even if user options
        # contain an SDK stderr setting.
        options_kwargs["stderr"] = capture_stderr
        if resume_session_id:
            options_kwargs["resume"] = resume_session_id
            options_kwargs["continue_conversation"] = False
            options_kwargs["fork_session"] = False
            options_kwargs.pop("session_id", None)
            prompt = (
                CONTINUE_RESUME_PROMPT
                if resume_reason == "continue"
                else PAUSE_RESUME_PROMPT
            )
        options = ClaudeAgentOptions(**options_kwargs)

        output_chunks: list[str] = []
        stop_reason = "completed"
        usage: dict[str, Any] = {}
        duration_ms = 0
        session_id = ""
        total_cost_usd: float | None = None

        interrupted: str | None = None
        execution_stopped = False

        def process_message(msg: Any) -> None:
            nonlocal session_id, usage, stop_reason, duration_ms, total_cost_usd
            message_session_id = self._extract_session_id(msg)
            if message_session_id and message_session_id != session_id:
                if resume_session_id and message_session_id != resume_session_id:
                    raise RuntimeError("SDK resumed a different session; refusing to continue")
                session_id = message_session_id
                self._emit_session_id(on_session_id, session_id)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        output_chunks.append(block.text)
                        self._emit_messages(on_message_update, output_chunks)
            elif isinstance(msg, ResultMessage):
                usage = msg.usage or {}
                stop_reason = msg.subtype
                duration_ms = msg.duration_ms
                total_cost_usd = msg.total_cost_usd
                if msg.result:
                    output_chunks.append(msg.result)
                    self._emit_messages(on_message_update, output_chunks)
                if msg.is_error and not interrupted:
                    result_detail = msg.result or "; ".join(msg.errors or []) or msg.subtype
                    raise RuntimeError(f"Claude Code returned an error result ({msg.subtype}): {result_detail}")

        try:
            if should_pause is None:
                stream: AsyncIterator[Any] = query(prompt=prompt, options=options)
                async for msg in stream:
                    if should_cancel is not None and should_cancel():
                        raise AgentTaskCancelledError("task cancelled while agent was running")
                    process_message(msg)
            else:
                # Receive and close the SDK on the same task (its transport uses
                # AnyIO cancel scopes). The monitor can interrupt even when the
                # agent is silent or blocked in a tool call.
                client = ClaudeSDKClient(options=options)
                try:
                    await client.connect()
                    await client.query(prompt)
                    async with anyio.create_task_group() as group:
                        async def monitor() -> None:
                            nonlocal interrupted
                            while True:
                                await anyio.sleep(CONTROL_POLL_SECONDS)
                                if should_pause():
                                    interrupted = "paused"
                                elif should_cancel is not None and should_cancel():
                                    interrupted = "cancelled"
                                if interrupted:
                                    with anyio.move_on_after(INTERRUPT_GRACE_SECONDS):
                                        await client.interrupt()
                                    await anyio.sleep(INTERRUPT_GRACE_SECONDS)
                                    group.cancel_scope.cancel()
                                    return

                        group.start_soon(monitor)
                        try:
                            async for msg in client.receive_response():
                                process_message(msg)
                        finally:
                            group.cancel_scope.cancel()
                finally:
                    try:
                        await client.disconnect()
                    except Exception as exc:
                        raise AgentTaskStopError(f"Agent 停止未完成：{exc}") from exc
                    execution_stopped = True
                # Disconnect has closed the SDK process before acknowledging pause.
                if interrupted == "paused":
                    raise AgentTaskPausedError("task paused")
                if interrupted == "cancelled":
                    raise AgentTaskCancelledError("task cancelled while agent was running")
            if should_cancel is not None and should_cancel():
                raise AgentTaskCancelledError("task cancelled while agent was running")
        except (AgentTaskCancelledError, AgentTaskPausedError, AgentTaskStopError):
            raise
        except Exception as exc:
            if execution_stopped and should_pause is not None and should_pause():
                raise AgentTaskPausedError("task paused after execution stopped") from exc
            while isinstance(exc, ExceptionGroup) and len(exc.exceptions) == 1:
                exc = exc.exceptions[0]
            cli_stderr = _bounded_stderr(stderr_lines)
            reported_stderr = getattr(exc, "stderr", None)
            is_placeholder = reported_stderr == "Check stderr output for details"
            if not cli_stderr and isinstance(reported_stderr, str) and not is_placeholder:
                cli_stderr = _redact_diagnostic_text(reported_stderr, self.settings.anthropic_api_key)
            error_type = type(exc).__name__
            base_message = str(exc).replace("\nError output: Check stderr output for details", "")
            message = f"{error_type}: {base_message}"
            if cli_stderr:
                message = f"{message}\nClaude CLI stderr:\n{cli_stderr}"
            partial_output = _redact_diagnostic_text("\n".join(output_chunks), self.settings.anthropic_api_key)
            if len(partial_output) > MAX_PARTIAL_OUTPUT_CHARS:
                partial_output = f"[earlier output truncated]\n{partial_output[-MAX_PARTIAL_OUTPUT_CHARS:]}"
            if partial_output and partial_output not in base_message:
                message = f"{message}\nAgent output before failure:\n{partial_output}"
            if len(message) > MAX_EXECUTION_ERROR_CHARS:
                message = f"{message[:MAX_EXECUTION_ERROR_CHARS]}\n[error details truncated]"
            raise ClaudeExecutionError(
                message,
                error_type=error_type,
                exit_code=getattr(exc, "exit_code", None),
                cli_stderr=cli_stderr,
            ) from exc
        logger.debug(
            "claude sdk stream finished",
            extra={"event_type": "claude_stream_end", "trace_id": session_id, "duration_ms": duration_ms},
        )

        return {
            "model": model,
            "agent_mode": agent_mode,
            "unattended": unattended,
            "output_text": "\n".join(
                chunk.strip() for chunk in output_chunks if chunk.strip()
            ).strip(),
            "stop_reason": stop_reason,
            "usage": usage,
            "duration_ms": duration_ms,
            "session_id": session_id,
            "total_cost_usd": total_cost_usd,
        }

    def _emit_messages(
        self, on_message_update: Callable[[list[str]], None] | None, output_chunks: list[str]
    ) -> None:
        if on_message_update is None:
            return
        try:
            normalized = [chunk.strip() for chunk in output_chunks if chunk.strip()]
            on_message_update(normalized)
        except Exception:
            logger.exception("on_message_update callback failed", extra={"event_type": "claude_context_callback_failed"})

    def _extract_session_id(self, message: Any) -> str:
        raw_session_id: Any = None
        if isinstance(message, SystemMessage):
            raw_session_id = message.data.get("session_id")
        if raw_session_id is None:
            raw_session_id = getattr(message, "session_id", None)
        if not isinstance(raw_session_id, str):
            return ""
        return raw_session_id.strip()

    def _emit_session_id(
        self, on_session_id: Callable[[str], None] | None, session_id: str
    ) -> None:
        if on_session_id is None:
            return
        try:
            on_session_id(session_id)
        except Exception:
            logger.exception(
                "on_session_id callback failed",
                extra={"event_type": "claude_session_callback_failed", "trace_id": session_id},
            )
