#!/usr/bin/env python3
"""Demo: 提交一个 Agent 任务，在工作目录创建 hello_world.txt。

脚本流程：
1) 调用 POST /v1/agent-tasks 创建任务
2) 轮询 GET /v1/agent-tasks/{task_id} 直到任务结束
3) 打印任务结果和任务日志，便于定位问题
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request


# 目标服务地址。可通过环境变量覆盖，方便在不同端口/环境复用脚本。
BASE_URL = os.getenv("AGENT_API_BASE_URL", "http://127.0.0.1:48010").rstrip("/")
# 如果服务开启了 API_TOKEN 鉴权，在这里读取并自动附加到请求头。
API_TOKEN = os.getenv("API_TOKEN", "")
# 轮询间隔（秒）：任务执行中每隔多久查询一次状态。
POLL_SECONDS = float(os.getenv("DEMO_POLL_SECONDS", "2"))
# 任务等待超时时间（秒）：超过该时间仍未结束则抛出超时错误。
TIMEOUT_SECONDS = int(os.getenv("DEMO_TIMEOUT_SECONDS", "180"))


def _request(method: str, path: str, data: dict | None = None) -> dict:
    """统一封装 HTTP 请求，返回 JSON 字典。

    - 自动拼接 BASE_URL
    - 自动附加 x-api-token（如果配置了 API_TOKEN）
    - 对 HTTP 错误做可读化包装，便于直接在终端排查
    """
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
    """提交“创建 hello_world.txt”任务，并返回 task_id。"""
    payload = {
        # 这里的 prompt 可以按你的业务场景替换。
        "prompt": (
            "Create a file named hello_world.txt in the current working directory. "
            "Its content must be exactly: hello world\\n. "
            "If the file already exists, overwrite it with that exact content. "
            "After writing, verify by reading the file once."
        ),
        # 默认使用 Agent 任务 + 无人值守模式。
        "agent_mode": True,
        "unattended": True,
        "claude_agent_options": {
            # 让 agent 在服务工作目录执行文件操作。
            "cwd": ".",
            "permission_mode": "bypassPermissions",
            "max_turns": 8,
            # 演示任务用到的最小工具集，可按需增减。
            "allowed_tools": ["Read", "Write", "Edit", "Glob", "Bash"],
        },
        # 自定义元数据，便于后续在日志/统计中识别 demo 任务。
        "metadata": {"demo": "create-hello-world"},
    }
    result = _request("POST", "/v1/agent-tasks", payload)
    task_id = result.get("task_id", "")
    if not task_id:
        raise RuntimeError(f"Missing task_id in response: {result}")
    return task_id


def wait_task(task_id: str) -> dict:
    """轮询任务状态，直到进入终态后返回任务详情。"""
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        task = _request("GET", f"/v1/agent-tasks/{task_id}")
        status = task.get("status")
        print(f"[poll] task={task_id} status={status}")
        # 终态集合与服务端状态机保持一致。
        if status in {"succeeded", "failed", "cancelled", "abandoned"}:
            return task
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"Task {task_id} not finished in {TIMEOUT_SECONDS}s")


def print_logs(task_id: str) -> None:
    """拉取并打印任务日志，方便查看执行过程。"""
    logs = _request("GET", f"/v1/agent-tasks/{task_id}/logs?offset=0&limit=100")
    print("\n=== task logs ===")
    for item in logs.get("items", []):
        print(f'{item.get("ts")} [{item.get("level")}] {item.get("event_type")}: {item.get("message")}')


def main() -> int:
    """主入口：提交任务 -> 等待完成 -> 打印结果和日志。"""
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
        # 统一在 stderr 输出错误，方便 CI 或脚本调用方识别失败。
        print(f"[demo error] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
