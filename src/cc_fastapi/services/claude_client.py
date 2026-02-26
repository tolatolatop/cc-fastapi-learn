import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
from cc_fastapi.core.config import get_settings


def validate_claude_agent_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Extension hook: currently pass-through without strict validation."""
    if options is None:
        return {}
    if not isinstance(options, dict):
        return {}
    return options


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
    ) -> dict[str, Any]:
        if not self.settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is missing")
        return asyncio.run(
            self._run_agent_task_async(
                prompt=prompt,
                model=model,
                metadata=metadata,
                claude_agent_options=claude_agent_options,
                agent_mode=agent_mode,
                unattended=unattended,
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
        allowed_tools = [
            item.strip()
            for item in self.settings.claude_allowed_tools.split(",")
            if item.strip()
        ]
        disallowed_tools = [
            item.strip()
            for item in self.settings.claude_disallowed_tools.split(",")
            if item.strip()
        ]

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
        if "env" in user_options and isinstance(user_options["env"], dict):
            merged_env = {**env, **user_options["env"]}
            merged_env["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key
            user_options = {**user_options, "env": merged_env}
        options_kwargs.update(user_options)

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
        async for msg in stream:
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        output_chunks.append(block.text)
            elif isinstance(msg, ResultMessage):
                usage = msg.usage or {}
                stop_reason = msg.subtype
                duration_ms = msg.duration_ms
                session_id = msg.session_id
                total_cost_usd = msg.total_cost_usd
                if msg.result:
                    output_chunks.append(msg.result)

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
