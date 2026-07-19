# Agent Queue

基于 FastAPI 的 Agent 任务队列，以及用于创建、监控和诊断任务的 React 控制台。

架构说明：[Workflow 与数据库概念](docs/workflows-and-database.md)。

## Docker Compose 启动

Compose 会自动读取项目根目录的 `.env`。首次部署可从示例文件复制后填写模型和 Webhook 配置：

```bash
cp .env.example .env
```

```bash
docker compose up --build
```

启动后可访问：

- Web 控制台：<http://localhost:18080>
- FastAPI 文档：<http://localhost:18000/docs>
- 健康检查：<http://localhost:18000/healthz>

可通过 `BACKEND_PORT` 和 `FRONTEND_PORT` 修改端口，并通过 `FRONTEND_HOST` 限制前端监听地址；`FRONTEND_HOST` 默认是 `0.0.0.0`。如果设置了 `API_TOKEN`，请在控制台右上角的“连接设置”中填写同一个 Token。

```bash
BACKEND_PORT=8000 FRONTEND_HOST=127.0.0.1 FRONTEND_PORT=8080 API_TOKEN=your-token docker compose up --build
```

## 前端本地开发

后端运行在 `localhost:18000` 时：

```bash
cd frontend
npm install
npm run dev
```

Vite 开发服务器会将 `/api` 请求代理到后端。生产镜像使用 Nginx 提供静态资源，并通过同源 `/api` 反向代理访问 Compose 中的 `app` 服务。
基础交互组件使用 React-Bootstrap，颜色、字号和圆角集中配置在
`frontend/src/bootstrap-theme.scss`；队列、Webhook 和检视轨道等业务可视化仍保留在自定义样式中。

## GitLab 与 GitHub Webhook

服务端接收地址：

```text
POST /v1/webhooks/gitlab
POST /v1/webhooks/github
```

可通过 `GITLAB_WEBHOOK_SECRET`、`GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH` 和 `GITLAB_WEBHOOK_QUEUE_NAME` 配置验证密钥、任务提示模板文件及目标队列。默认模板位于 `config/templates/gitlab_webhook_prompt.j2`。控制台中的“Webhook 档案”页面可按项目、分支、事件 UUID、Webhook UUID 或关联任务 ID 检索最近的触发记录，并查看原始 Payload。

GitHub 使用对应的 `GITHUB_WEBHOOK_SECRET`、`GITHUB_WEBHOOK_PROMPT_TEMPLATE_PATH` 和
`GITHUB_WEBHOOK_QUEUE_NAME`。在 GitHub Webhook 设置中选择 `application/json`，把 Payload URL
指向 `/v1/webhooks/github`，并让 Secret 与 `GITHUB_WEBHOOK_SECRET` 保持一致。服务按原始请求体校验
`X-Hub-Signature-256`，使用 `X-GitHub-Delivery` 去重；GitHub Enterprise Server 实例地址从
`X-GitHub-Enterprise-Host` 记录，普通 GitHub 记录为 `https://github.com`。默认 Prompt 模板位于
`config/templates/github_webhook_prompt.j2`。

两个平台的 Jinja 模板都可直接读取 Payload 顶层字段，并额外提供 `payload`、`event_type` 和
`webhook`。控制台支持按平台、事件类型、项目、分支、投递 UUID 或关联任务 ID 检索归档。

服务始终原样归档平台 Payload，并通过只读的 `WebhookPayload` 投影统一解析仓库、操作者、分支及
PR/MR 基本信息。平台差异集中在 GitHub/GitLab Adapter；仓库同步、工作流关联和控制台摘要只读取
标准化投影。Webhook 列表响应中的 `parsed_payload` 提供这份投影，`payload` 字段仍保留原始内容。

已归档的 GitLab/GitHub Webhook 仓库可同步到仓库管理目录：

```text
POST /v1/repositories/sync
```

接口按平台和仓库路径去重，只创建尚未登记的仓库，并将新仓库的 Tags 初始化为空；已有仓库及
其 Tags 不会被修改。同步接口与其他仓库 API 一样使用 `X-API-Token` 鉴权，也可通过控制台
“仓库管理”页面右上角的“同步 Webhook 仓库”按钮触发。

## 工作流扩展

Webhook 事件由工作流注册表匹配，经过 `before` 规划后创建零个、一个或多个任务；任务进入终态后执行 `after_task`。运行、步骤和任务关联分别保存在 `workflow_runs`、`workflow_step_runs` 和 `workflow_task_links` 中。

当前 `GitLabPromptTaskWorkflow` 和 `GitHubPromptTaskWorkflow` 都基于平台无关的
`WebhookPromptTaskWorkflow`，负责读取 Jinja 模板、拼接 Prompt 并创建 Agent Task。新增平台时增加
入口鉴权、平台工作流和配置即可复用统一的触发、幂等、归档和列表逻辑。具体事件工作流使用更高
优先级注册后，会先于平台兜底工作流匹配。

GitLab Merge Request 事件会按“项目路径 + MR IID”写入 `workflow_correlations`。收到 `object_attributes.action=update` 的事件时，新工作流会在同一事务内取消该 MR 尚在排队或执行中的旧任务，并把旧工作流标记为 `superseded`；已完成的历史任务不会被修改。

GitHub Pull Request 事件会按“仓库全名 + PR 编号”写入同一关联表；收到 `action=synchronize`
时采用相同的替换规则。

内部服务可按 PR/MR 列出最近变更请求，并精确读取任务结果和完整工作流历史：

```text
GET /v1/internal/change-requests?provider=github&state=open&offset=0&limit=20
GET /v1/internal/change-requests/detail?provider=github&project_path=org/project&pr_number=42&task_status=succeeded
GET /v1/internal/gitlab/merge-request-tasks?project_path=group/project&merge_request_iid=123
```

列表按不同 PR/MR 分页；详情包含无 Task 的 skipped/failed Workflow，并可按 Task、Workflow、Role、
活跃状态和时间过滤。旧 GitLab 路由作为兼容入口保留。接口与任务 API 一样通过
`X-API-Token` 使用 `API_TOKEN` 鉴权。

## Agent 管理 CLI

安装项目后可使用 PR-centric 管理命令。客户端连接配置与服务端 `API_TOKEN` 分离：

```bash
export CC_FASTAPI_BASE_URL=http://localhost:18000
export CC_FASTAPI_TOKEN=your-token
```

```text
cc-fastapi-admin pr recent --limit 10
cc-fastapi-admin pr show github org/project 42 --task-status succeeded
cc-fastapi-admin pr collect github org/project 42 --input issues.json
cc-fastapi-admin pr verify github org/project 42 --input results.json
```

`collect` 输入为 `{"issues": [...]}`，支持最多 500 个问题；未指定 `--task-id` 时只使用该 PR
最新的 active+succeeded Task。`verify` 输入为
`{"results": [{"issue_no": 1, "status": "accepted", "note": "..."}]}`，在批次内把
`issue_no` 映射为内部 UUID，单次最多处理 500 条。两条写命令都支持 `--input -` 从 stdin
读取，重试时会读取最终状态并进行幂等校验。

## 检视问题统计 API

检视结果回收使用 `review_issue_batches` 记录一次回收和合入后验证流程，使用
`review_issues` 保存 Agent 提取的问题。接口只承担自动化数据采集与统计，不提供问题管理操作台。
控制台侧边栏的“检视统计”页面可查看采纳率、筛选回收批次、录入批次及问题，并在 PR 合入后
记录 `accepted`、`not_accepted` 验证结论。

```text
POST  /v1/review-issue-batches
GET   /v1/review-issue-batches
GET   /v1/review-issue-batches/{batch_id}
PATCH /v1/review-issue-batches/{batch_id}
POST  /v1/review-issue-batches/{batch_id}/issues
PATCH /v1/review-issue-batches/{batch_id}/issues

GET   /v1/review-issues
GET   /v1/review-issues/{issue_id}
PATCH /v1/review-issues/{issue_id}
GET   /v1/review-issues/summary
```

创建批次后，批量写入提取结果会把批次推进到 `waiting_merge`；设置 `merged_sha` 并进入
`verifying` 后，可以逐条或批量写入 `accepted`、`not_accepted` 验证结果。全部问题完成验证时，
批次自动变为 `completed`。零问题批次同样可以完成回收并计入汇总。列表接口支持按仓库、PR、
状态、等级和创建时间筛选，汇总接口返回问题总数、已验证数、接受数、采纳率和等级分布。

## 列表分页

任务和 Webhook 列表均使用服务端分页，默认每页 20 条，单页最大 200 条：

```text
GET /v1/agent-tasks?offset=0&limit=20&status=queued&queue=default&q=prompt
GET /v1/webhooks?offset=0&limit=20&provider=github&event_type=pull_request&q=project
```

响应中的 `total` 是当前筛选条件下的记录总数，`summary` 提供不受筛选影响的全局状态、队列或事件类型摘要。任务接口可重复传递 `status` 参数以同时查询多个状态。
