from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from cc_fastapi.core.config import get_settings
from cc_fastapi.services import claude_client as claude_client_module
from cc_fastapi.services.claude_client import ClaudeClient


def test_claude_client_uses_agent_options(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")
    monkeypatch.setenv("CLAUDE_MAX_TURNS", "7")
    monkeypatch.setenv("CLAUDE_CWD", ".")
    monkeypatch.setenv("CLAUDE_ALLOWED_TOOLS", "Read,Edit")
    monkeypatch.setenv("CLAUDE_DISALLOWED_TOOLS", "Bash")
    get_settings.cache_clear()

    captured: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        captured["prompt"] = prompt
        captured["options"] = options
        yield AssistantMessage(content=[TextBlock(text="hello from sdk")], model="claude-test")
        yield ResultMessage(
            subtype="end_turn",
            duration_ms=123,
            duration_api_ms=100,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            total_cost_usd=0.12,
            usage={"input_tokens": 10, "output_tokens": 20},
            result="final result",
        )

    monkeypatch.setattr(claude_client_module, "query", fake_query)

    client = ClaudeClient()
    result = client.run_agent_task(
        prompt="do work",
        model="claude-test",
        metadata={"job": "abc"},
        agent_mode=True,
        unattended=True,
    )

    options = captured["options"]
    assert captured["prompt"] == "do work"
    assert getattr(options, "model") == "claude-test"
    assert getattr(options, "permission_mode") == "bypassPermissions"
    assert getattr(options, "max_turns") == 7
    assert getattr(options, "allowed_tools") == ["Read", "Edit"]
    assert getattr(options, "disallowed_tools") == ["Bash"]
    assert result["output_text"] == "hello from sdk\nfinal result"
    assert result["stop_reason"] == "end_turn"
    assert result["usage"]["input_tokens"] == 10

