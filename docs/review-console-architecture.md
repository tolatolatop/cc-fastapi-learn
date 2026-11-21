# 独立检视操作台设计

## 系统边界

检视生成系统（`cc_fastapi`）是检视意见及状态历史的唯一事实来源。独立操作台由
`review-console/backend` 和 `review-console/frontend` 组成，使用自己的用户数据库，且不直接连接
主应用数据库。浏览器只访问操作台后端；服务间通过 `/v1/review-console/*` 契约通信。

```text
企业身份源 ──OIDC──> 浏览器 ──用户会话──> 操作台后端 ──服务令牌 + 操作者身份──> 主应用 API
                                              │                                  │
                                      身份、用户、仓库授权库                意见、追加式审计库
```

这种划分让两个系统能分别部署和扩缩容，也避免向浏览器暴露主应用的服务令牌。主应用不接受
浏览器声明的角色；操作台先执行仓库授权，再以受信任的服务身份调用主应用。

## 权限模型

- `admin`：管理用户及仓库授权，默认拥有所有仓库的修改权限。
- `read`：只允许查看指定 `provider + project_path` 下的意见与历史。
- `write`：包含只读能力，并可修改指定仓库下意见的状态及说明。
- `all_repositories_read`（全仓库只读）：账号级开关，一键覆盖当前及后续新增的全部仓库只读访问，
  不需要逐仓库维护授权。判定顺序为 `admin` → 命中的单仓库授权 → 全仓库只读，因此对某个仓库单独
  授予 `write` 仍然有效，而 `write` 之外的其他仓库回落到只读。
- 非管理员没有隐式写入权限。停用用户后，已有会话也会在下一次请求时失效。

全仓库只读在“角色管理”页以单个开关切换，通过 `PATCH /v1/admin/users/{user_id}` 的
`all_repositories_read` 字段提交；统计与评审记录导出对这类账号也使用全局范围，无需逐仓库列举。
新注册账号（管理员创建的本地账号和 SSO/OAuth 首次登录建档的账号）默认开启该权限，由
`REVIEW_CONSOLE_DEFAULT_ALL_REPOSITORIES_READ` 控制，默认值为 `true`；管理员创建账号时也可以显式
传入 `all_repositories_read=false`。已有账号不会被启动迁移改动，仍需管理员按需一键开启或关闭。

仓库是权限的最小边界。访问控制代码独立位于 `review_console.access_control`，其中会话依赖、OIDC
协议、身份建档、角色与仓库策略、管理接口彼此分层；意见裁定业务只依赖 `current_user` 和仓库策略，
不处理登录协议或用户管理。授权存放在操作台数据库，不耦合代码托管平台的成员模型。

OIDC SSO 使用 Authorization Code + PKCE。回调校验签名、issuer、audience、state 和 nonce；首次登录
按 issuer + subject 创建独立身份映射及本地用户，并按部署配置初始化全仓库只读开关。系统不会因用户名
或邮箱相同而自动绑定已有本地账号。可配置一个 IdP 组映射为管理员；未配置管理员组时，SSO 用户的管理员
角色仍由操作台维护。本地账号登录可在 SSO 验证完成后关闭。

## 状态与审计

人工裁定使用独立四态契约：`unverified`（待分析）、`accepted`（接受）、`not_accepted`（拒绝）和
`needs_info`（待补充）。拒绝必须选择标准原因分类并填写详细理由。操作台不再提供进入待补充状态的按钮，
但会继续展示和筛选已有的待补充记录，并允许将其改为待分析、接受或拒绝。
自动修复验证继续使用独立的 `verification_status`，不会被人工裁定接口改写。每次真实变化都会在
主应用写入 `review_issue_status_changes`，记录维度、前后状态、前后原因、操作者 ID/名称、来源和
UTC 时间；历史 API 只读，不提供更新或删除入口。

意见审核页以 PR/MR 为一级导航，跨授权仓库汇总队列。每个 PR/MR 都有可复制的直达地址，格式为
`?view=issues&provider=<provider>&project_path=<project_path>&pr=<number>`；登录后刷新或打开该地址会恢复到对应
PR/MR 的检视意见列表，无权限或目标不存在时明确提示，不回退到其他 PR/MR。

意见列表默认只展示该 PR/MR 最新一次检视提交（最新批次）的意见。历史批次的意见默认隐藏，
“历史意见”开关可展开全部批次；顶部统计、完成状态和仓库待分析数默认按最新批次聚合，
`history_issue_total` 给出被隐藏的历史意见数量，`batch_total` 给出该 PR/MR 的提交次数。由于每份
检视报告都全量录入为独立批次，最新批次即当轮完整结论，无需增量提交或与历史结果去重。

“最新一次”按代码版本先后判定，而不是按批次入库时间。批次上的 `head_committed_at` 记录被检视
head 提交的提交时间，排序键为 `head_committed_at → 批次 created_at → 批次 id`。写命令把版本
定位参数作为必填项：`pr collect` 与 `pr add-issues` 都要求 `--head-sha`（7–64 位十六进制）与
`--head-committed-at`（ISO-8601，未来时间被拒绝），采集方据此显式声明被检视版本。主应用接口对
旧调用方保持可选字段，缺失时回落到该次检视工作流关联平台事件的时间，再回落到批次创建时间；
历史批次没有该值时行为与按入库时间排序一致。因此补录、重放或先收后录的旧报告不会冒充最新版本，
接口同时回传 `latest_head_committed_at` 以便核对。

审核队列在前端支持按仓库路径、PR/MR 编号和代码平台地址搜索；以空格分隔的关键字采用全部匹配，
搜索结果将仍有待分析意见的 PR/MR 排在前面。意见列表默认按文件路径、行号排序；审核者可切换
“待分析优先”，同组内仍按文件位置排序。文件位置完整显示，并以 `PATH:LINENO` 形式复制；详细信息
和可编辑说明统一在中央弹窗中处理。变更历史不再作为顶层页面，从详情按钮打开独立只读弹窗，记录
按 UTC 创建时间从新到旧排序，并在固定高度区域内滚动。

状态更新携带 `expected_updated_at`。若另一名审核者已修改同一意见，主应用返回 `409`，客户端必须
刷新后再裁定，防止后写覆盖先写。重复提交完全相同的状态、原因和说明是幂等操作，不产生虚假历史。

PR/MR 完成状态由主应用聚合检视批次和意见后返回，默认只聚合最新批次（`latest_only=false` 时聚合
全部批次）：`processing` 表示仍在生成或等待零问题流程结束，`pending` 表示仍有待分析意见，
`completed` 表示全部意见已裁定，`no_issues` 表示流程完成且没有意见，`failed` 表示最新批次相关
流程均失败。`needs_info` 与 `unverified` 都计入待分析；前端不通过当前分页结果猜测完成状态。

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
- `GET /v1/review-console/review-records`

批次响应带 `head_committed_at`，`pull-request` 响应带 `latest_head_committed_at`，可用于确认“最新”
对应的代码版本时间。

`repositories`、`pull-requests`、`pull-request` 和 `issues` 都接受 `latest_only` 查询参数，默认
`true`：只统计最新批次的意见与状态；传 `false` 时回到跨全部批次的历史视图。`issues/{id}` 不受该
参数影响，历史批次中的意见仍可直接读取和裁定。

写入接口另外要求 `X-Review-Actor-Id` 和 `X-Review-Actor-Name`。操作台对浏览器提供 `/v1/auth/*`、
`/v1/repositories`、`/v1/issues/*`、`/v1/statistics`、`/v1/review-records` 与 `/v1/admin/users/*`，
并在转发前校验仓库权限。
非管理员统计请求会显式携带全部授权仓库范围；管理员和开启全仓库只读的账号可读取全局统计。时间过滤
以当前最终裁定时间为准，贡献按意见的当前最终裁定人计数，因此反复修改不会放大贡献。

认证接口额外包含公开的 `GET /v1/auth/config`、发起登录的 `GET /v1/auth/sso/login` 和 OIDC 回调
`GET /v1/auth/sso/callback`。前端只根据公开配置决定显示 SSO 或本地登录，不会读取客户端密钥。
启用 OAuth 2.0 Bearer 后，所有原本接受会话 Cookie 的受保护接口也接受标准
`Authorization: Bearer <access-token>`。服务通过同一身份源的 Discovery 与 JWKS 校验 JWT Access
Token 的签名、issuer、audience 和有效期，再按 `iss + sub` 映射到同一套用户、角色及仓库权限。

## 部署

在仓库根目录复制 `.env.example` 为 `.env`，生成至少 32 字节的随机会话密钥和独立服务令牌，不要
与 `API_TOKEN` 共用。启用 SSO 时至少设置以下值：

```bash
REVIEW_CONSOLE_SSO_ENABLED="true"
REVIEW_CONSOLE_SSO_ISSUER_URL="https://id.example.com/realms/company"
REVIEW_CONSOLE_SSO_CLIENT_ID="review-console"
REVIEW_CONSOLE_SSO_CLIENT_SECRET="<client-secret>"
REVIEW_CONSOLE_SSO_REDIRECT_URI="https://review.example.com/api/v1/auth/sso/callback"
REVIEW_CONSOLE_OAUTH_BEARER_ENABLED="true"
REVIEW_CONSOLE_OAUTH_AUDIENCE="review-console-api"
REVIEW_CONSOLE_OAUTH_TOKEN_TYPE="at+jwt"
REVIEW_CONSOLE_SSO_ADMIN_GROUP="review-console-admins"
REVIEW_CONSOLE_COOKIE_SECURE="true"
docker compose --profile review-console up --build
```

回调地址必须与身份源注册值完全一致。默认客户端认证方式是 `client_secret_basic`，也支持
`client_secret_post` 和配合 PKCE 的 `none`。声明名称、scope、签名算法、按钮文案和超时均可通过
`.env.example` 中列出的环境变量调整。确认 SSO 和至少一个管理员账号可用后，可设置
`REVIEW_CONSOLE_LOCAL_LOGIN_ENABLED=false` 关闭密码登录。

新注册账号的默认仓库读权限由 `REVIEW_CONSOLE_DEFAULT_ALL_REPOSITORIES_READ` 控制（默认 `true`），
即本地新建账号与 SSO/OAuth 首次建档账号都默认拥有全仓库只读权限：

```dotenv
REVIEW_CONSOLE_DEFAULT_ALL_REPOSITORIES_READ="true"
```

需要收紧默认值时设为 `false`，之后在“角色管理”页按账号一键开启。旧版本创建的数据库在启动时由
`review_console.schema.apply_schema_migrations` 补上 `all_repositories_read` 列，已有账号保持原有
授权不变。

主控制台仍在 `18080`，独立操作台默认在 `18090`。首次启动且数据库中没有管理员时，环境变量中的
初始账号会创建管理员；已有管理员时不会覆盖密码。生产环境应在首次登录后轮换初始密码变量，使用
TLS，并将两个后端部署在私有网络中。备份时必须同时备份两个数据库：主库保存审计事实，操作台库
保存身份和授权。

## 身份与会话安全

业务会话使用 `HttpOnly + SameSite=Strict` 的签名短期 Cookie，OIDC 临时流程使用十分钟有效的签名
`HttpOnly + SameSite=Lax` Cookie，以支持身份源跨站回跳；密码使用 PBKDF2-SHA256。生产 TLS 环境必须
设置 `REVIEW_CONSOLE_COOKIE_SECURE=true`。客户端密钥、会话密钥和服务令牌只从环境变量读取，不进入
前端构建产物或数据库。

完整功能优先级和当前完成度见
[`review-console-roadmap.md`](review-console-roadmap.md)。
