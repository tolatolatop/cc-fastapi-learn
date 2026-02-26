from typing import Any

from anthropic import Anthropic

from cc_fastapi.core.config import get_settings


class ClaudeClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = Anthropic(api_key=self.settings.anthropic_api_key) if self.settings.anthropic_api_key else None

    def run_agent_task(
        self,
        *,
        prompt: str,
        model: str,
        metadata: dict[str, Any] | None,
        agent_mode: bool = True,
        unattended: bool = True,
    ) -> dict[str, Any]:
        if not self._client:
            raise RuntimeError("ANTHROPIC_API_KEY is missing")

        system_note = (
            "You are running in Claude Agent task mode."
            f" agent_mode={str(agent_mode).lower()}, unattended={str(unattended).lower()}."
        )
        response = self._client.messages.create(
            model=model,
            max_tokens=2000,
            system=system_note,
            messages=[{"role": "user", "content": prompt}],
            metadata=metadata or {},
        )
        content = []
        for item in response.content:
            text = getattr(item, "text", "")
            if text:
                content.append(text)
        return {
            "model": model,
            "agent_mode": agent_mode,
            "unattended": unattended,
            "output_text": "\n".join(content).strip(),
            "stop_reason": response.stop_reason,
            "usage": {
                "input_tokens": getattr(response.usage, "input_tokens", 0),
                "output_tokens": getattr(response.usage, "output_tokens", 0),
            },
        }

