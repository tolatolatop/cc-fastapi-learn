# 任务停止钩子（Stop Hook）

停止钩子在 Agent **正常停止**、但任务尚未标记为 `succeeded` 之前执行。它把“Agent 认为自己完成了”
和“任务真的完成”分开：只有钩子放行，Worker 才写入成功状态并释放执行权。

当前内置钩子为 `review_report`：

1. Agent 正常停止后，Worker 调用停止钩子。
2. 钩子检查元数据指定的检视报告文件是否存在、是否为普通文件、是否非空、是否在本次运行中重新写入。
3. 检查通过后，把报告文件内容发布到对应 PR/MR 评论区。
4. 发布成功后任务才进入 `succeeded`；否则任务继续保持运行语义。

## 元数据开关

钩子默认关闭，完全由任务元数据控制。最小写法：

```json
{
  "hooks": {
    "review_report": true
  }
}
```

或：

```json
{
  "hooks": {
    "review_report": {
      "enabled": true
    }
  }
}
```

只写 `enabled` 时，报告路径、续跑次数、新鲜度校验、评论发布全部走默认值；评论目标自动从 Webhook
任务元数据组装。关闭钩子：

```json
{"hooks": {"review_report": false}}
```

兼容的顶层别名：`review_report`、`review_report_hook`。推荐始终使用 `hooks.review_report`。

### 默认参数

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true`（条目存在时） | `false` 时完全跳过钩子 |
| `report_path` / `path` | `reports/review-report-{task_id}.md` | 相对路径基于 `CLAUDE_CWD` 或任务的 `claude_agent_options.cwd`，也支持绝对路径；支持 `{task_id}` 占位符 |
| `max_continues` / `max_retries` | `3` | 最多自动续跑次数，取值 0–10 |
| `require_fresh` | `true` | 报告文件 mtime 必须不早于本次运行开始时间（2 秒容差） |
| `publish_to_comment` / `upload_to_comment` | `true` | 有评论目标时发布到 PR/MR 评论；没有目标时只做文件检查并记录 `comment_target_missing` |
| `require_comment_target` | `false` | 为 `true` 时，缺少评论目标直接失败；默认允许降级为只检查文件 |
| `comment_max_chars` | `60000` | 评论内报告正文上限；超出时截断并标注 |
| `comment` | 自动 | 评论目标；也接受 `provider`、`project_path`、`pr_number`/`pull_request`/`number`、`instance_url` 直接写在钩子条目上 |

### 评论目标自动组装

Webhook 工作流创建任务时，以下字段已经自动写入任务元数据：

- `metadata.trigger`：例如 `gitlab_webhook`
- `metadata.gitlab.project_path` / `metadata.github.project_path`
- `metadata.gitlab.change_request_number` / `metadata.github.change_request_number`（PR/MR 事件）
- `metadata.gitlab.instance_url` / `metadata.github.instance_url`

因此 PR/MR 触发的检视任务只需要 `{"hooks": {"review_report": true}}`，不需要重复填写 provider、
仓库和 PR 编号。显式 `comment` 只在覆盖自动值时使用：

```json
{
  "hooks": {
    "review_report": {
      "enabled": true,
      "comment": {
        "provider": "gitlab",
        "project_path": "group/project",
        "number": "42",
        "instance_url": "https://gitlab.example.com"
      }
    }
  }
}
```

没有 Webhook 上下文的普通 API 任务默认只检查报告文件，不发布评论；如需强制要求评论目标，设置
`"require_comment_target": true`。

### 由工作流注入元数据

`WebhookPromptTaskWorkflow` 支持在事件配置中提供 `task_metadata`，它会与平台元数据一起写入任务：

```python
event.config["task_metadata"] = {"hooks": {"review_report": True}}
```

通过 `POST /v1/agent-tasks` 创建任务时，直接放在请求的 `metadata` 字段即可。

## 检查与续跑

- 文件不存在、不是文件、为空、早于本次运行：钩子返回可重试失败。
- Worker 不标记成功，而是通过 `resume` 在同一会话中续跑，`continuation_count` 加一，并把失败原因
  作为续跑提示传给 Agent；Agent 会收到报告文件的精确绝对路径。
- 状态仍是 `queued`，`resume_pending=true`，`resume_mode=continue`。下一次 Worker claim 会复用
  原会话 `session_id`。
- 最多自动续跑 `max_continues` 次（默认 3 次）。第 4 次仍不满足时任务标记为 `failed`，错误信息
  保留最后一次钩子反馈，并写入 `hook_failed` 日志。
- 没有评论目标且 `require_comment_target=false`：跳过发布，任务继续成功，结果标记
  `comment_target_missing=true`；设置 `require_comment_target=true` 则立即失败。
- 有评论目标但发布调用失败属于配置/外部接口错误，不消耗自动续跑次数，任务立即 `failed`，避免
  无意义地让模型重写报告。
- 钩子自身异常会记录 `task_stop_hook_crashed` 并立即失败，避免任务永久停留在 `running`。

任务结果中的 `hook_results` 保存本次钩子结论：

```json
{
  "enabled": true,
  "hook": "review_report",
  "report_path": "/workspace/reports/review-report-<task-id>.md",
  "published": true,
  "provider": "gitlab",
  "comment_id": "12345",
  "comment_url": "https://gitlab.example.com/group/project/-/merge_requests/42#note_12345"
}
```

任务日志新增事件：`hook_continue`、`hook_failed`。Worker 结构化日志新增
`task_stop_hook_continue`、`task_stop_hook_failed`、`task_stop_hook_crashed`。

## 评论发布

`CommentPublisher` 直接调用平台 API：

- GitLab：`POST {instance}/api/v4/projects/{urlencoded project_path}/merge_requests/{number}/notes`
- GitHub：`POST {api}/repos/{owner}/{repo}/issues/{number}/comments`

所需环境变量：

```bash
GITLAB_API_TOKEN=""          # PRIVATE-TOKEN，需要 api scope
GITLAB_API_URL=""            # 可选，默认使用事件 instance_url 或 https://gitlab.com
GITHUB_API_TOKEN=""          # Bearer token，需要 issues:write
GITHUB_API_URL=""            # 可选，默认 https://api.github.com 或企业版 {instance}/api/v3
```

评论包含任务 ID、会话 ID、报告文件名和报告正文；开头写入
`<!-- cc-fastapi:review-report:{task_id} -->` 标记，便于人工识别。

## 代码位置

- `src/cc_fastapi/hooks/base.py`：停止钩子协议、上下文和结论。
- `src/cc_fastapi/hooks/config.py`：从任务元数据解析开关和配置。
- `src/cc_fastapi/hooks/review_report.py`：报告文件检查与发布。
- `src/cc_fastapi/hooks/engine.py`：钩子求值、续跑提示和结果序列化。
- `src/cc_fastapi/services/comment_publisher.py`：GitLab/GitHub 评论发布。
- `src/cc_fastapi/services/worker.py`：在 `mark_success` 之前执行钩子。
- `src/cc_fastapi/services/queue.py`：`schedule_hook_continuation` 与 `mark_hook_failed`。
