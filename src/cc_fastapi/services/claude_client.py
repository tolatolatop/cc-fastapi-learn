import asyncio
import json
import logging
import os
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any, Callable

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
from cc_fastapi.core.config import get_settings

logger = logging.getLogger(__name__)


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
        if self.settings.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = self.settings.anthropic_base_url
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

        options = ClaudeAgentOptions(
            **options_kwargs
        )

        output_chunks: list[str] = []
        stop_reason = "completed"
        usage: dict[str, Any] = {}
        duration_ms = 0
        session_id = ""
        total_cost_usd: float | None = None

        stream: AsyncIterator[Any] = query(prompt=prompt, options=options)
        logger.debug("claude sdk stream started", extra={"event_type": "claude_stream_start"})
        async for msg in stream:
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        output_chunks.append(block.text)
                        self._emit_messages(on_message_update, output_chunks)
            elif isinstance(msg, ResultMessage):
                usage = msg.usage or {}
                stop_reason = msg.subtype
                duration_ms = msg.duration_ms
                session_id = msg.session_id
                total_cost_usd = msg.total_cost_usd
                if msg.result:
                    output_chunks.append(msg.result)
                    self._emit_messages(on_message_update, output_chunks)
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
