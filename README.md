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

可通过 `BACKEND_PORT` 和 `FRONTEND_PORT` 修改端口。如果设置了 `API_TOKEN`，请在控制台右上角的“连接设置”中填写同一个 Token。

```bash
BACKEND_PORT=8000 FRONTEND_PORT=8080 API_TOKEN=your-token docker compose up --build
```

## 前端本地开发

后端运行在 `localhost:18000` 时：

```bash
cd frontend
npm install
npm run dev
```

Vite 开发服务器会将 `/api` 请求代理到后端。生产镜像使用 Nginx 提供静态资源，并通过同源 `/api` 反向代理访问 Compose 中的 `app` 服务。

## GitLab Webhook

服务端接收地址：

```text
POST /v1/webhooks/gitlab
```

可通过 `GITLAB_WEBHOOK_SECRET`、`GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH` 和 `GITLAB_WEBHOOK_QUEUE_NAME` 配置验证密钥、任务提示模板文件及目标队列。默认模板位于 `config/templates/gitlab_webhook_prompt.j2`。控制台中的“Webhook 档案”页面可按项目、分支、事件 UUID、Webhook UUID 或关联任务 ID 检索最近的触发记录，并查看原始 Payload。

## 工作流扩展

Webhook 事件由工作流注册表匹配，经过 `before` 规划后创建零个、一个或多个任务；任务进入终态后执行 `after_task`。运行、步骤和任务关联分别保存在 `workflow_runs`、`workflow_step_runs` 和 `workflow_task_links` 中。

当前默认工作流是 `GitLabPromptTaskWorkflow`，负责读取 Jinja 模板、拼接 Prompt 并创建 Agent Task。新增业务工作流时继承 `Workflow`，实现 `matches()` 与 `before()`，需要后处理时覆盖 `after_task()`，然后在 `build_default_workflow_engine()` 中注册。默认 GitLab 工作流的优先级最低，因此具体事件工作流会优先匹配。

GitLab Merge Request 事件会按“项目路径 + MR IID”写入 `workflow_correlations`。收到 `object_attributes.action=update` 的事件时，新工作流会在同一事务内取消该 MR 尚在排队或执行中的旧任务，并把旧工作流标记为 `superseded`；已完成的历史任务不会被修改。

内部服务可按 MR 精确读取关联任务的输入、结果、上下文和工作流信息：

```text
GET /v1/internal/gitlab/merge-request-tasks?project_path=group/project&merge_request_iid=123
```

接口支持 `offset`、`limit`，并与任务 API 一样通过 `X-API-Token` 使用 `API_TOKEN` 鉴权。
