from pathlib import Path

import pytest
from claude_agent_sdk._errors import ProcessError
from claude_agent_sdk.types import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from cc_fastapi.core.config import get_settings
from cc_fastapi.services import claude_client as claude_client_module
from cc_fastapi.services.claude_client import AgentTaskCancelledError, ClaudeClient, ClaudeExecutionError


def test_claude_client_uses_agent_options(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://aihubmix.com/")
    monkeypatch.setenv("API_TIMEOUT_MS", "3000000")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")
    monkeypatch.setenv("CLAUDE_MAX_TURNS", "7")
    monkeypatch.setenv("CLAUDE_CWD", ".")
    monkeypatch.setenv("CLAUDE_ALLOWED_TOOLS", '["Read","Edit"]')
    monkeypatch.setenv("CLAUDE_DISALLOWED_TOOLS", '["Bash"]')
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
    session_ids: list[str] = []
    result = client.run_agent_task(
        prompt="do work",
        model="claude-test",
        metadata={"job": "abc"},
        claude_agent_options={"max_turns": 3, "permission_mode": "plan"},
        agent_mode=True,
        unattended=True,
        on_session_id=session_ids.append,
    )

    options = captured["options"]
    assert captured["prompt"] == "do work"
    assert getattr(options, "model") == "claude-test"
    assert getattr(options, "permission_mode") == "plan"
    assert getattr(options, "max_turns") == 3
    assert getattr(options, "allowed_tools") == ["Read", "Edit"]
    assert getattr(options, "disallowed_tools") == ["Bash"]
    assert getattr(options, "env")["ANTHROPIC_BASE_URL"] == "https://aihubmix.com"
    assert getattr(options, "env")["API_TIMEOUT_MS"] == "3000000"
    assert getattr(options, "env")["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-sonnet-4-5"
    assert getattr(options, "env")["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-4-5"
    assert getattr(options, "env")["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-4-5"
    assert result["output_text"] == "hello from sdk\nfinal result"
    assert result["stop_reason"] == "end_turn"
    assert result["usage"]["input_tokens"] == 10
    assert session_ids == ["session-1"]


def test_claude_client_creates_missing_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    captured: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        captured["options"] = options
        yield AssistantMessage(content=[TextBlock(text="ok")], model="claude-test")
        yield ResultMessage(
            subtype="end_turn",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            usage={},
            result="ok",
        )

    monkeypatch.setattr(claude_client_module, "query", fake_query)

    monkeypatch.chdir(tmp_path)
    target_cwd = tmp_path / "nested" / "workdir"
    assert not target_cwd.exists()

    client = ClaudeClient()
    client.run_agent_task(
        prompt="make cwd",
        model="claude-test",
        metadata=None,
        claude_agent_options={"cwd": "nested/workdir"},
        agent_mode=True,
        unattended=True,
    )

    assert target_cwd.exists()
    options = captured["options"]
    assert Path(getattr(options, "cwd")).resolve() == target_cwd.resolve()


def test_claude_client_rejects_absolute_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    client = ClaudeClient()

    with pytest.raises(ValueError, match="relative path"):
        client.run_agent_task(
            prompt="bad cwd",
            model="claude-test",
            metadata=None,
            claude_agent_options={"cwd": str((tmp_path / "abs").resolve())},
            agent_mode=True,
            unattended=True,
        )


def test_claude_client_calls_on_message_update(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    async def fake_query(*, prompt, options, transport=None):
        yield AssistantMessage(content=[TextBlock(text="hello")], model="claude-test")
        yield AssistantMessage(content=[TextBlock(text="world")], model="claude-test")
        yield ResultMessage(
            subtype="end_turn",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            usage={},
            result="done",
        )

    monkeypatch.setattr(claude_client_module, "query", fake_query)

    updates: list[list[str]] = []
    client = ClaudeClient()
    client.run_agent_task(
        prompt="stream",
        model="claude-test",
        metadata=None,
        agent_mode=True,
        unattended=True,
        on_message_update=lambda messages: updates.append(messages),
    )

    assert updates
    assert updates[-1] == ["hello", "world", "done"]


def test_claude_client_reports_session_id_from_init_message(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    async def fake_query(*, prompt, options, transport=None):
        yield SystemMessage(subtype="init", data={"session_id": "session-from-init"})
        yield AssistantMessage(content=[TextBlock(text="hello")], model="claude-test")
        yield ResultMessage(
            subtype="end_turn",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session-from-init",
            usage={},
            result="done",
        )

    monkeypatch.setattr(claude_client_module, "query", fake_query)

    session_ids: list[str] = []
    result = ClaudeClient().run_agent_task(
        prompt="session",
        model="claude-test",
        metadata=None,
        agent_mode=True,
        unattended=True,
        on_session_id=session_ids.append,
    )

    assert session_ids == ["session-from-init"]
    assert result["session_id"] == "session-from-init"


def test_claude_client_captures_and_redacts_cli_stderr(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-test-key")
    get_settings.cache_clear()

    async def fake_query(*, prompt, options, transport=None):
        assert options.stderr is not None
        options.stderr("startup rejected for secret-test-key")
        raise ProcessError(
            "Command failed with exit code 1",
            exit_code=1,
            stderr="Check stderr output for details",
        )
        yield

    monkeypatch.setattr(claude_client_module, "query", fake_query)

    with pytest.raises(ClaudeExecutionError) as captured:
        ClaudeClient().run_agent_task(
            prompt="fail",
            model="claude-test",
            metadata=None,
        )

    error = captured.value
    assert error.error_type == "ProcessError"
    assert error.exit_code == 1
    assert error.cli_stderr == "startup rejected for [REDACTED_API_KEY]"
    assert "Claude CLI stderr" in str(error)
    assert "secret-test-key" not in str(error)


def test_claude_client_reports_error_result_detail(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    async def fake_query(*, prompt, options, transport=None):
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=0,
            session_id="session-error",
            usage={},
            result="API Error: invalid endpoint",
        )

    monkeypatch.setattr(claude_client_module, "query", fake_query)

    with pytest.raises(ClaudeExecutionError, match="API Error: invalid endpoint") as captured:
        ClaudeClient().run_agent_task(
            prompt="fail",
            model="claude-test",
            metadata=None,
        )

    assert captured.value.error_type == "RuntimeError"


def test_claude_client_stops_stream_when_task_is_cancelled(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    async def fake_query(*, prompt, options, transport=None):
        yield AssistantMessage(content=[TextBlock(text="should not be processed")], model="claude-test")

    monkeypatch.setattr(claude_client_module, "query", fake_query)

    client = ClaudeClient()
    with pytest.raises(AgentTaskCancelledError, match="task cancelled"):
        client.run_agent_task(
            prompt="stream",
            model="claude-test",
            metadata=None,
            agent_mode=True,
            unattended=True,
            should_cancel=lambda: True,
        )
