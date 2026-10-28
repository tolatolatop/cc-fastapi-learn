from pathlib import Path

import pytest
from claude_agent_sdk._errors import ProcessError
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from cc_fastapi.core.config import get_settings
from cc_fastapi.services import claude_client as claude_client_module
from cc_fastapi.services.claude_client import (
    AgentTaskCancelledError,
    ClaudeClient,
    ClaudeExecutionError,
)


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


@pytest.mark.parametrize('interrupt_returns_result', [True, False])
def test_pause_interrupts_silent_agent_and_closes_before_ack(monkeypatch, interrupt_returns_result):
    import anyio

    from cc_fastapi.services.claude_client import AgentTaskPausedError

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    monkeypatch.setattr(claude_client_module, 'CONTROL_POLL_SECONDS', 0.01)
    monkeypatch.setattr(claude_client_module, 'INTERRUPT_GRACE_SECONDS', 0.02)
    get_settings.cache_clear()
    events = []

    class FakeClient:
        def __init__(self, *, options):
            self.interrupted = anyio.Event()

        async def connect(self):
            pass

        async def disconnect(self):
            events.append('closed')

        async def query(self, prompt):
            events.append('started')

        async def receive_response(self):
            yield SystemMessage(subtype='init', data={'session_id': 'saved-session'})
            await self.interrupted.wait()
            if not interrupt_returns_result:
                await anyio.sleep_forever()
            yield ResultMessage(
                subtype='error_during_execution', duration_ms=1, duration_api_ms=1,
                is_error=True, num_turns=1, session_id='saved-session',
                usage={}, result='interrupted',
            )

        async def interrupt(self):
            events.append('interrupt')
            self.interrupted.set()

    monkeypatch.setattr(claude_client_module, 'ClaudeSDKClient', FakeClient)
    session_ids = []
    with pytest.raises(AgentTaskPausedError):
        ClaudeClient().run_agent_task(
            prompt='do work', model='test', metadata=None,
            should_pause=lambda: True, on_session_id=session_ids.append,
        )
    assert events == ['started', 'interrupt', 'closed']
    assert session_ids == ['saved-session']


def test_resume_passes_exact_session_without_replaying_original_prompt(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    get_settings.cache_clear()
    captured = {}

    class FakeClient:
        def __init__(self, *, options):
            captured['options'] = options

        async def connect(self):
            pass

        async def disconnect(self):
            captured['closed'] = True

        async def query(self, prompt):
            captured['prompt'] = prompt

        async def receive_response(self):
            yield ResultMessage(
                subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id='saved-session', usage={}, result='done',
            )

    monkeypatch.setattr(claude_client_module, 'ClaudeSDKClient', FakeClient)
    result = ClaudeClient().run_agent_task(
        prompt='original instructions', model='test', metadata=None,
        should_pause=lambda: False, resume_session_id='saved-session',
        claude_agent_options={'fork_session': True, 'continue_conversation': True},
    )
    assert captured['options'].resume == 'saved-session'
    assert not captured['options'].fork_session
    assert not captured['options'].continue_conversation
    assert captured['prompt'] != 'original instructions'
    assert captured['closed']
    assert result['session_id'] == 'saved-session'


@pytest.mark.parametrize('pause', [False, True])
def test_real_sdk_control_protocol_resume_and_interrupt(monkeypatch, pause):
    """Exercise the installed SDK with a local transport, without an API call."""
    import json

    import anyio
    from claude_agent_sdk import ClaudeSDKClient

    from cc_fastapi.services.claude_client import AgentTaskPausedError

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    monkeypatch.setattr(claude_client_module, 'CONTROL_POLL_SECONDS', 0.01)
    get_settings.cache_clear()
    events = []

    class Transport:
        def __init__(self):
            self.sender, self.receiver = anyio.create_memory_object_stream(20)

        async def connect(self):
            events.append('connected')

        async def write(self, data):
            msg = json.loads(data)
            if msg['type'] == 'control_request':
                subtype = msg['request']['subtype']
                events.append(subtype)
                await self.sender.send({
                    'type': 'control_response', 'response': {
                        'subtype': 'success', 'request_id': msg['request_id'], 'response': {},
                    },
                })
                if subtype == 'interrupt':
                    await self.send_result()
            elif msg['type'] == 'user':
                await self.sender.send({
                    'type': 'system', 'subtype': 'init', 'session_id': 'saved-session',
                })
                if not pause:
                    await self.send_result()

        async def send_result(self):
            await self.sender.send({
                'type': 'result', 'subtype': 'success', 'duration_ms': 1,
                'duration_api_ms': 1, 'is_error': False, 'num_turns': 1,
                'session_id': 'saved-session', 'usage': {}, 'result': 'done',
            })

        async def read_messages(self):
            async for msg in self.receiver:
                yield msg

        async def close(self):
            events.append('closed')
            await self.sender.aclose()

        async def end_input(self):
            pass

        def is_ready(self):
            return True

    def make_client(*, options):
        assert options.resume == 'saved-session'
        return ClaudeSDKClient(options=options, transport=Transport())

    monkeypatch.setattr(claude_client_module, 'ClaudeSDKClient', make_client)
    params = {
        'prompt': 'original task', 'model': 'test', 'metadata': None,
        'should_pause': lambda: pause, 'resume_session_id': 'saved-session',
    }
    if pause:
        with pytest.raises(AgentTaskPausedError):
            ClaudeClient().run_agent_task(**params)
        assert 'interrupt' in events
    else:
        assert ClaudeClient().run_agent_task(**params)['output_text'] == 'done'
    assert 'initialize' in events
    assert events[-1] == 'closed'
