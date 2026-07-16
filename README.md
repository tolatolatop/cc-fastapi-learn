# Agent Queue

基于 FastAPI 的 Agent 任务队列，以及用于创建、监控和诊断任务的 React 控制台。

## Docker Compose 启动

```bash
docker compose up --build
```

启动后可访问：

- Web 控制台：<http://localhost:18080>
- FastAPI 文档：<http://localhost:18000/docs>
- 健康检查：<http://localhost:18000/healthz>

可通过 `FRONTEND_PORT` 修改前端端口。如果设置了 `API_TOKEN`，请在控制台右上角的“连接设置”中填写同一个 Token。

```bash
FRONTEND_PORT=8080 API_TOKEN=your-token docker compose up --build
```

## 前端本地开发

后端运行在 `localhost:18000` 时：

```bash
cd frontend
npm install
npm run dev
```

Vite 开发服务器会将 `/api` 请求代理到后端。生产镜像使用 Nginx 提供静态资源，并通过同源 `/api` 反向代理访问 Compose 中的 `app` 服务。
