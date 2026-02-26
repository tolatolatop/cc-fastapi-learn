#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.error
import urllib.request


BASE_URL = os.getenv("AGENT_API_BASE_URL", "http://127.0.0.1:48010").rstrip("/")
API_TOKEN = os.getenv("API_TOKEN", "")
POLL_SECONDS = float(os.getenv("DEMO_POLL_SECONDS", "2"))
TIMEOUT_SECONDS = int(os.getenv("DEMO_TIMEOUT_SECONDS", "180"))


def _request(method: str, path: str, data: dict | None = None) -> dict:
    body = None
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["x-api-token"] = API_TOKEN
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from exc


def submit_task() -> str:
    payload = {
        "prompt": (
            "Create a file named hello_world.txt in the current working directory. "
            "Its content must be exactly: hello world\\n. "
            "If the file already exists, overwrite it with that exact content. "
            "After writing, verify by reading the file once."
        ),
        "agent_mode": True,
        "unattended": True,
        "claude_agent_options": {
            "cwd": ".",
            "permission_mode": "bypassPermissions",
            "max_turns": 8,
            "allowed_tools": ["Read", "Write", "Edit", "Glob", "Bash"],
        },
        "metadata": {"demo": "create-hello-world"},
    }
    result = _request("POST", "/v1/agent-tasks", payload)
    task_id = result.get("task_id", "")
    if not task_id:
        raise RuntimeError(f"Missing task_id in response: {result}")
    return task_id


def wait_task(task_id: str) -> dict:
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        task = _request("GET", f"/v1/agent-tasks/{task_id}")
        status = task.get("status")
        print(f"[poll] task={task_id} status={status}")
        if status in {"succeeded", "failed", "cancelled", "abandoned"}:
            return task
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"Task {task_id} not finished in {TIMEOUT_SECONDS}s")


def print_logs(task_id: str) -> None:
    logs = _request("GET", f"/v1/agent-tasks/{task_id}/logs?offset=0&limit=100")
    print("\n=== task logs ===")
    for item in logs.get("items", []):
        print(f'{item.get("ts")} [{item.get("level")}] {item.get("event_type")}: {item.get("message")}')


def main() -> int:
    print(f"Submitting task to: {BASE_URL}")
    task_id = submit_task()
    print(f"Task created: {task_id}")
    task = wait_task(task_id)
    print("\n=== final task ===")
    print(json.dumps(task, ensure_ascii=False, indent=2))
    print_logs(task_id)
    return 0 if task.get("status") == "succeeded" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[demo error] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
