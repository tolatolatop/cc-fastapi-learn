# 独立检视裁定台设计

## 系统边界

检视生成系统（`cc_fastapi`）是检视意见及状态历史的唯一事实来源。独立裁定台由
`review-console/backend` 和 `review-console/frontend` 组成，使用自己的用户数据库，且不直接连接
主应用数据库。浏览器只访问裁定台后端；服务间通过 `/v1/review-console/*` 契约通信。

```text
浏览器 ──用户会话──> 裁定台后端 ──服务令牌 + 操作者身份──> 主应用 API
                         │                                  │
                  用户、仓库授权库                    意见、追加式审计库
```

这种划分让两个系统能分别部署和扩缩容，也避免向浏览器暴露主应用的服务令牌。主应用不接受
浏览器声明的角色；裁定台先执行仓库授权，再以受信任的服务身份调用主应用。

## 权限模型

- `admin`：管理用户及仓库授权，默认拥有所有仓库的修改权限。
- `read`：只允许查看指定 `provider + project_path` 下的意见与历史。
- `write`：包含只读能力，并可修改指定仓库下意见的状态及说明。
- 非管理员没有隐式权限。停用用户后，已有会话也会在下一次请求时失效。

仓库是权限的最小边界。授权存放在裁定台数据库，不耦合代码托管平台的成员模型，后续可以在
这一层接入 OIDC/LDAP 组映射而不修改主应用。

## 状态与审计

人工裁定使用独立四态契约：`unverified`（待裁定）、`accepted`（接受）、`not_accepted`（拒绝）和
`needs_info`（待补充）。拒绝必须选择标准原因分类并填写详细理由；待补充可先单击设置状态，再从意见详情补写所需信息。
自动修复验证继续使用独立的 `verification_status`，不会被人工裁定接口改写。每次真实变化都会在
主应用写入 `review_issue_status_changes`，记录维度、前后状态、前后原因、操作者 ID/名称、来源和
UTC 时间；历史 API 只读，不提供更新或删除入口。

意见审核页以 PR/MR 为一级导航，跨授权仓库汇总队列。每个 PR/MR 都有可复制的直达地址，格式为
`?view=issues&provider=<provider>&project_path=<project_path>&pr=<number>`；登录后刷新或打开该地址会恢复到对应
PR/MR 的检视意见列表，无权限或目标不存在时明确提示，不回退到其他 PR/MR。

审核队列在前端支持按仓库路径、PR/MR 编号和代码平台地址搜索；以空格分隔的关键字采用全部匹配，
搜索结果将仍有待裁定意见的 PR/MR 排在前面。意见列表默认按文件路径、行号排序；审核者可切换
“待裁定优先”，同组内仍按文件位置排序。文件位置完整显示，并以 `PATH:LINENO` 形式复制；详细信息
和可编辑说明统一在中央弹窗中处理。

状态更新携带 `expected_updated_at`。若另一名审核者已修改同一意见，主应用返回 `409`，客户端必须
刷新后再裁定，防止后写覆盖先写。重复提交完全相同的状态、原因和说明是幂等操作，不产生虚假历史。

PR/MR 完成状态由主应用聚合其全部检视批次和意见后返回：`processing` 表示仍在生成或等待零问题
流程结束，`pending` 表示仍有待裁定意见，`completed` 表示全部意见已裁定，`no_issues` 表示流程
完成且没有意见，`failed` 表示所有相关流程均失败。`needs_info` 与 `unverified` 都计入待裁定；前端
不通过当前分页结果猜测完成状态。

## 接口契约

主应用集成接口使用 `X-Review-Console-Token`：

- `GET /v1/review-console/repositories`
- `GET /v1/review-console/pull-requests`
- `GET /v1/review-console/pull-request`
- `GET /v1/review-console/issues`
- `GET /v1/review-console/issues/{id}`
- `PUT /v1/review-console/issues/{id}/status`
- `GET /v1/review-console/issues/{id}/history`
- `GET /v1/review-console/statistics`

写入接口另外要求 `X-Review-Actor-Id` 和 `X-Review-Actor-Name`。裁定台对浏览器提供 `/v1/auth/*`、
`/v1/repositories`、`/v1/issues/*`、`/v1/statistics` 与 `/v1/admin/users/*`，并在转发前校验仓库权限。
非管理员统计请求会显式携带全部授权仓库范围；管理员可读取全局统计。时间过滤以当前最终裁定时间
为准，贡献按意见的当前最终裁定人计数，因此反复修改不会放大贡献。

## 部署

生成至少 32 字节的随机会话密钥和独立服务令牌，不要与 `API_TOKEN` 共用：

```bash
export REVIEW_CONSOLE_API_TOKEN='<random-service-token>'
export REVIEW_CONSOLE_SESSION_SECRET='<random-session-secret-at-least-32-chars>'
export REVIEW_CONSOLE_ADMIN_PASSWORD='<initial-admin-password>'
docker compose --profile review-console up --build
```

主控制台仍在 `18080`，独立裁定台默认在 `18090`。首次启动且数据库中没有管理员时，环境变量中的
初始账号会创建管理员；已有管理员时不会覆盖密码。生产环境应在首次登录后轮换初始密码变量，使用
TLS，并将两个后端部署在私有网络中。备份时必须同时备份两个数据库：主库保存审计事实，裁定台库
保存身份和授权。

## 后续演进

当前会话使用 `HttpOnly + SameSite=Strict` 的签名短期 Cookie、密码使用 PBKDF2-SHA256；生产 TLS 环境
应设置 `REVIEW_CONSOLE_COOKIE_SECURE=true`。接入企业身份源时可替换裁定台认证层；权限表、
主应用契约与审计模型无需改变。后续新增修复跟踪等业务状态时，应继续使用独立字段并显式定义状态
迁移，而不是复用人工裁定枚举。

完整功能优先级和当前完成度见
[`review-console-roadmap.md`](review-console-roadmap.md)。
