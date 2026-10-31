# 任务「继续执行」设计

状态：设计稿（未实现）

## 1. 问题

任务被标记为 `succeeded`，但实际执行过程被模型侧中断（输出到一半、工具调用后没有收尾、
长任务提前结束），工作并没有真正完成。现状只有「重新执行」：

- `POST /v1/agent-tasks/{id}/retry` 会调用 `queue.retry_task()` **新建一个 task**，
  新 task 没有 `session_id`，worker 第一次 claim 时会把 `session_id` 置空，等于从零开始；
- 新建 task 还会产生新的 workflow link、新的日志与新的 UI 记录，原任务的历史被割裂。

目标：对**已完成（succeeded）**任务**一键继续**，复用**同一个 task 记录**和**同一个 Claude session**，
在已有对话上下文上接着做。

## 2. 现有可复用能力（关键）

暂停/恢复链路已经实现了「同一 session 续跑」，继续执行直接复用：

| 环节 | 现有实现 |
| --- | --- |
| session 持久化 | `agent_tasks.session_id`（`AgentTask.session_id`） |
| 上下文持久化 | `agent_task_contexts.messages_json`，按消息追加 |
| 恢复标记 | `agent_tasks.resume_pending` |
| claim 时保留会话 | `TaskQueueService.claim_next_task()`：`session_id=candidate.session_id if resuming else None` |
| 执行时恢复 | `WorkerManager._run_task()` → `resume_session_id=task.session_id` |
| SDK 层 | `ClaudeClient._run_agent_task_async()`：`options.resume=session_id`、`continue_conversation=False`、**不重放原始 prompt** |
| 防串会话 | 恢复时若 SDK 返回的 session_id 与传入不一致，直接抛错 |

所以「继续执行」= 把已完成任务**原子地重新排入队列**并打上 resume 标记，不需要新的执行引擎。

## 3. 方案总览

- **原地续跑**：不新建 task，保持 `task.id`、`session_id`、`payload.prompt`、日志、workflow link 不变。
- **两个新列**：`resume_mode`（`pause` / `continue`）与 `continuation_count`。
  - 只用 `resume_pending` 无法区分「暂停后恢复」和「终态后继续」，两者的续跑指令语义不同；
  - `continuation_count` 是轻量审计与 UI 展示，避免复用 `attempt` 污染重试预算。
- **一条 CAS UPDATE 完成状态跃迁**：并发点击只有一个成功，天然幂等。
- **worker 按 mode 选择续跑指令**；继续执行失败落到 `failed`，不会掉进全局暂停态，
  也不会触发「新 session 自动重试」；失败后按既定策略走「重新执行」。
- **前端一个按钮**：详情抽屉底部「继续执行」，点击即发请求，无二次确认。

### 状态机

```
succeeded（仅此一种终态可继续，且必须有 session_id）
                    │
                    │ POST /{id}/continue（CAS）
                    ▼
      queued（全局暂停时 → paused）
      resume_pending=1, resume_mode=continue, continuation_count+1
                    │
                    │ worker claim（保留 session_id，刷新 started_at / running_expire_at）
                    ▼
                running
                 ├── 成功 → succeeded（清 resume 标记，session 不变，result 覆盖）
                 └── 失败 → failed（清 resume 标记、保留 session 记录；失败后走「重新执行」）
```

## 4. 数据模型

`src/cc_fastapi/db/models.py`：

```python
class TaskResumeMode(StrEnum):
    PAUSE = "pause"
    CONTINUE = "continue"


# 只允许「已完成」任务继续；failed/cancelled/abandoned 可能仍有在途执行，
# 也可能是明确失败，统一走「重新执行」。
CONTINUABLE_TASK_STATUSES = (TaskStatus.SUCCEEDED,)
```

`AgentTask` 新增：

```python
resume_mode: Mapped[TaskResumeMode | None] = mapped_column(
    Enum(TaskResumeMode, native_enum=False, length=16), nullable=True
)
continuation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

迁移 `src/cc_fastapi/db/migrations.py`（沿用现有 add-column 模式，纯增量）：

```python
_add_column(engine, table_name="agent_tasks", model_column=AgentTask.__table__.c.resume_mode)
_add_column(
    engine, table_name="agent_tasks", model_column=AgentTask.__table__.c.continuation_count,
    definition_suffix=" NOT NULL DEFAULT 0",
)
```

老库自动补列，默认为 `NULL` / `0`，不需要数据回填。

## 5. 后端改动

### 5.1 `TaskQueueService.continue_task()`（`services/queue.py`）

```python
def continue_task(self, db: Session, task_id: str) -> AgentTask | None:
    self._begin_sqlite_write_transaction(db)          # SQLite 跨进程串行化
    control = self._lock_control(db)                   # 与 enqueue/claim/pause 同一把锁
    task = db.scalars(
        select(AgentTask).where(AgentTask.id == task_id)
        .with_for_update().execution_options(populate_existing=True)
    ).first()
    if task is None:
        db.rollback()
        return None
    if task.status not in CONTINUABLE_TASK_STATUSES:
        db.rollback()
        raise TaskControlConflict("任务仍在队列或执行中，无需继续执行")
    if not task.session_id:
        db.rollback()
        raise TaskControlConflict("该任务没有可续跑的会话，请使用重新执行")

    now = utc_now()
    paused = control.paused
    previous_status = task.status
    next_count = task.continuation_count + 1
    result = db.execute(
        update(AgentTask)
        .where(
            AgentTask.id == task_id,
            AgentTask.status == task.status,                  # CAS，双击只有一次成功
        )
        .values(
            status=TaskStatus.PAUSED if paused else TaskStatus.QUEUED,
            paused_at=now if paused else None,
            resume_pending=True,
            resume_mode=TaskResumeMode.CONTINUE,
            continuation_count=AgentTask.continuation_count + 1,
            execution_token=None,        # 与上一轮执行彻底解绑
            worker_id=None,
            error_message=None,
            finished_at=None,
            started_at=None,             # 由 claim 写入本轮开始时间
            running_expire_at=None,
            running_remaining_seconds=None,
            queue_expire_at=now + timedelta(hours=self.settings.queue_ttl_hours),
            scheduled_at=now,
            # result / attempt / payload 保持不变
        )
    )
    if result.rowcount == 0:
        db.rollback()
        raise TaskControlConflict("任务状态已变化，请刷新后重试")
    self.log_event(
        db, task_id, "INFO", "continued", "task continued in the same session",
        {"previous_status": previous_status.value,
         "continuation_count": next_count,
         "session_id": task.session_id, "queue_name": task.queue_name},
    )
    db.commit()
    db.refresh(task)
    return task
```

要点：

- `attempt` / `max_attempts` 不动。继续执行是人工动作，不占自动重试预算，避免出现 `4/3`。
- `result` 保留到本轮成功后被覆盖；继续失败时仍能看到上一次结果。
- 全局队列暂停时直接落 `paused`，与 `create_task` 行为一致；`resume_all` 不需要改动。

### 5.2 `claim_next_task()` 小改

继续执行时 `started_at` 已被清空，若沿用现有 `started_at=candidate.started_at if resuming else now`
会得到 `NULL`，耗时显示异常。改为：

```python
started_at=candidate_task.started_at if resuming and candidate_task.started_at else now,
```

继续执行已经把 `started_at` 清空，首次 claim 会写入新时间；普通的暂停恢复仍保留原
`started_at`，与现有的「暂停不丢开始时间」行为一致。

`session_id` / `attempt` / `running_expire_at` 逻辑不变（`running_remaining_seconds` 已清空 → 拿满额 TTL）。

### 5.3 `mark_*` 语义

- `mark_success()`：额外清 `resume_mode=None`（已有 `resume_pending=False`）。
- `mark_paused()`：不需要改。继续执行中途被全局暂停时 `resume_mode` 保持 `continue`，
  恢复后仍用 continue 指令（「先核对已完成的部分、不要重复副作用」对暂停恢复同样适用）。
- `mark_retry_or_failed()`：清 `resume_mode=None`（自动重试是全新会话）。
- 新增 `mark_continuation_failed()`：

```python
def mark_continuation_failed(
    self, db: Session, task_id: str, error_message: str, *, execution_token: str | None = None
) -> bool:
    result = db.execute(
        update(AgentTask).where(
            AgentTask.id == task_id,
            AgentTask.status == TaskStatus.RUNNING,
            *([AgentTask.execution_token == execution_token] if execution_token else []),
        ).values(
            status=TaskStatus.FAILED, error_message=error_message, finished_at=utc_now(),
            resume_pending=False, resume_mode=None, execution_token=None,
            worker_id=None, running_expire_at=None,
        )
    )
    if result.rowcount == 0:
        db.rollback()
        return False
    self.log_event(db, task_id, "ERROR", "failed", "continuation failed",
                   {"continuation": True})
    db.commit()
    return True
```

保留 `session_id` 作为审计事实；由于只有 `succeeded` 可以继续，失败后入口是「重新执行」。

### 5.4 `WorkerManager._run_task()`

```python
resuming = task.resume_pending
continuing = resuming and task.resume_mode == TaskResumeMode.CONTINUE
...
result = self.client.run_agent_task(
    ...,
    resume_session_id=task.session_id if resuming else None,
    resume_reason="continue" if continuing else ("pause" if resuming else None),
)
```

异常分支（`except Exception`，先 `db.rollback()`；`continuing` 在第 150 行的 `resuming`
旁边一次性捕获，避免 rollback 后再读状态）：

```python
if continuing:
    self.queue.mark_continuation_failed(db, task_id, str(exc), execution_token=execution_token)
    self.workflows.handle_task_terminal(db, task_id)
    return
if resuming:
    self.queue.mark_paused(db, task_id, execution_token, error=str(exc))
    return
```

- `AgentTaskPausedError` / `AgentTaskCancelledError` / `AgentTaskStopError` 分支保持现状
  （全局暂停、取消、停止失败仍走原语义）。
- 继续失败后调用 `handle_task_terminal`，让关联 workflow 收敛（通常是已经终态的 run，直接 no-op）。

### 5.5 `ClaudeClient` 续跑指令

`run_agent_task(..., resume_reason: Literal["pause", "continue"] | None = None)`，
透传到 `_run_agent_task_async`（用 `Literal` 而不是 ORM 枚举，保持 client 与 models 解耦；
`TaskResumeMode` 是 `StrEnum`，`worker` 传 `task.resume_mode.value` 即可）：

```python
if resume_session_id:
    options_kwargs["resume"] = resume_session_id
    options_kwargs["continue_conversation"] = False
    options_kwargs["fork_session"] = False
    options_kwargs.pop("session_id", None)
    if resume_reason == "continue":
        prompt = CONTINUE_PROMPT
    else:
        prompt = PAUSE_PROMPT
```

- `PAUSE_PROMPT` = 现有文案，保持不变，避免行为回归。
- `CONTINUE_PROMPT` 新增（放在模块常量区）：

```
The previous run was marked finished, but it may have stopped before the work was
actually complete. Treat the existing conversation as the source of truth. First
verify what has already been done (files, commands, tool results, external side
effects) and do not repeat completed side-effecting operations. Then finish the
remaining work and return the final required result.
```

- `on_session_id` 中「SDK 返回了不同 session 就报错」的保护继续保留，这是「保持同一个 session」
  的硬约束。

### 5.6 API 与 schema

`src/cc_fastapi/schemas/tasks.py`：

```python
class TaskContinueResponse(BaseModel):
    task_id: str
    status: TaskStatus
    queue_name: str
    session_id: str | None
    continuation_count: int
```

`TaskItemResponse` 增加 `continuation_count: int`（`_to_task_item()` 补字段）。

`src/cc_fastapi/api/tasks.py`：

```python
@router.post("/{task_id}/continue", response_model=TaskContinueResponse,
             dependencies=[Depends(require_token)])
def continue_task(task_id: str, db: TaskDb) -> TaskContinueResponse:
    try:
        task = queue.continue_task(db, task_id)
    except TaskControlConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    logger.info("task continued via api",
                extra={"event_type": "api_continue_task", "task_id": task.id,
                       "queue_name": task.queue_name})
    return TaskContinueResponse(...)
```

不调用 `workflow_engine.retry_task`：继续执行不新建任务、不改 workflow link。

## 6. API 契约

```
POST /api/v1/agent-tasks/{task_id}/continue
Authorization: Bearer <token>
Content-Length: 0
```

| 场景 | 状态码 | 说明 |
| --- | --- | --- |
| `succeeded` + 有 session | 200 | `{task_id, status: queued\|paused, queue_name, session_id, continuation_count}` |
| 任务不存在 | 404 | `task not found` |
| 任务不是 `succeeded`（含 failed/cancelled/abandoned） | 409 | 只有已完成的任务可以继续执行 |
| 已完成但没有 `session_id` | 409 | 该任务没有可续跑的会话，请使用重新执行 |
| 并发第二次点击 | 409 | 只有已完成的任务可以继续执行（或：任务状态已变化，请刷新后重试） |

200 不代表已开始执行，只代表已成功入队；前端靠既有 5s/3.5s 轮询刷新状态。

## 7. 前端

- `frontend/src/api.ts`：

```ts
continueTask: (id: string) =>
  request<TaskContinueResponse>(`/v1/agent-tasks/${id}/continue`, { method: 'POST' }),
```

- `frontend/src/types.ts`：`TaskItem` 加 `continuation_count: number`；新增 `TaskContinueResponse`。
- `frontend/src/App.tsx` `DetailDrawer`：
  - `canContinue = task.status === 'succeeded'`；
  - 底部操作区新增主按钮（`Play` 图标已引入）：

```tsx
{canContinue && (
  <Button variant="primary" onClick={() => onContinue(task.id)}
          disabled={continuing || !task.session_id}
          title={task.session_id ? undefined : '该任务没有可续跑的会话'}>
    <Play size={16} />{continuing ? '正在加入队列' : '继续执行'}
  </Button>
)}
```

  - 「重新执行」降级为 `outline-secondary`，保留；
  - 提示文案：「继续执行复用同一个会话，不会创建新任务」；
  - **无确认弹窗**（一键触发）；请求期间按钮禁用；成功后 toast
    「已加入队列，第 N 次继续执行」并立即 `loadDetail(id)`。
- 任务列表不动；抽屉是唯一入口，避免误触。

## 8. 不变量与并发

| 编号 | 不变量 |
| --- | --- |
| I1 | 继续执行不改变 `task.id`、`session_id`、`payload.prompt`，不新建 task/link |
| I2 | `succeeded` → 队列态的迁移只能由 CAS UPDATE 完成；重复点击仅一次成功 |
| I3 | 只有 `succeeded` 可以继续；failed/cancelled/abandoned 一律走「重新执行」，避免复活可能有在途进程或已明确失败的任务 |
| I4 | 继续执行失败 → `failed`，不回退 `paused`，不触发新 session 自动重试 |
| I5 | 全局暂停优先：继续执行同样落 `paused`，由「全部恢复」统一释放 |
| I6 | `resume_mode` 描述本轮执行是否来源于人工继续；成功、继续失败、自动重试后清空，暂停恢复保留 |
| I7 | 续跑必须命中同一 session；SDK 返回不同 session 立即失败 |

竞态：

| 竞态 | 处理 |
| --- | --- |
| 双击 / 多端同时点 | `_lock_control` + CAS `status=读取值`，第二个 409 |
| 点击与 `mark_success` 的 `finally: release_execution` 交错 | `succeeded` 时 SDK 已返回；continue 把 token 置空，旧 `release_execution` 因 token 不匹配而跳过，不会误清新执行 |
| 点击与 `handle_task_terminal` 交错 | 任务已变 active，`handle_task_terminal` 直接返回；workflow 收敛推迟到本轮结束，语义不变 |
| claim 与全局暂停 | 现有 `_lock_control` 保证，continue 复用同一把锁 |
| 暂停发生在继续执行过程中 | 现有 `AgentTaskPausedError` → `paused`，恢复后继续沿用 continue 指令 |

## 9. 边界情况

1. **SDK 会话已过期/丢失**：`resume` 报错 → 任务 `failed`，错误信息展示在详情页，用户可改用「重新执行」。
2. **workflow 关联任务**：沿用同一 task/link，已终态的 workflow run 不会重复推进；
   `after_task:{task_id}` 步骤已存在时直接跳过。副作用（例如 PR 评论）由续跑指令要求「先核对再重试」。
3. **上下文持续增长**：继续执行会继续向 `messages_json` 追加。暂不裁剪；后续可加保留上限。
4. **已完成但没有 session**（例如成功路径没有写回 session）：按钮置灰，提示改用「重新执行」。
5. **`result` 归属**：继续执行期间展示的是上一次结果，成功后覆盖；失败则保留，便于对比。
6. **自动识别「假完成」**：不做自动判定（不可靠）。可选增强：`succeeded` 且
   `result.output_text` 为空时在按钮旁提示「本次输出为空，建议继续执行」——列出但不纳入首版。
7. **多实例部署**：迁移是幂等加列；状态迁移靠数据库锁，不依赖进程内状态。

## 10. 测试计划

新增 `tests/test_task_continue.py`（沿用 `test_task_pause.py` 的 session fixture）：

1. 成功任务继续：`queued`、`resume_pending=True`、`resume_mode=continue`、`session_id`/`prompt` 不变、
   `continuation_count+1`、`attempt` 不变、`running_remaining_seconds` 清空、日志有 `continued`。
2. queued/running 继续 → `TaskControlConflict`，任务字段不变；failed 同样拒绝。
3. 无 session 的 succeeded 继续 → 409/冲突，状态不变。
4. 全局暂停下继续 → `paused`，`resume_all` 后 `queued` 且 session 保留。
5. **并发回归**：`ThreadPoolExecutor` + `Barrier` 同时 continue 同一任务，只有一次成功，
   `continuation_count == 1`。
6. worker 续跑：mock `ClaudeClient.run_agent_task`，断言 `resume_session_id` 为原 session、
   `resume_reason == continue`、上下文为「旧消息 + 新消息」；成功后标记清空。
7. 续跑失败：任务 `failed`、`session_id` 保留、`resume_pending=False`，不进入 `paused`。

其它：

- `tests/test_tasks_api.py`：200 正常路径、404、409（执行中/无 session）、缺 token。
- `tests/test_claude_client.py`：continue 模式使用新指令且 `options.resume == 原 session`；
  pause 模式文案不变。
- `tests/test_db_migrations.py`：老表补列、重复执行幂等、`continuation_count` 默认 0。
- `cd frontend && npm run build`；手工点一次按钮验证轮询与 toast。

## 11. 实施顺序

| 阶段 | 内容 | 预估 |
| --- | --- | --- |
| 1 | models + migration + `continue_task` + API/schema | 1.5h |
| 2 | worker / claude_client 的 resume_mode 与本轮语义 | 1h |
| 3 | 前端按钮 + api/types | 0.5h |
| 4 | 测试、`ruff check`、`pytest -q`、前端构建、文档 | 1.5h |

回滚：代码回退即可；新增列对旧代码无影响（SQLAlchemy 忽略未映射列）。

## 12. 已采用的默认值（如需调整请说明）

1. 可继续的状态：仅 `succeeded`（已确认）。failed/cancelled/abandoned 一律走「重新执行」。
2. 续跑失败落 `failed`（保留 session 记录），不自动重试、也不进入 `paused`；之后用「重新执行」兜底。
3. 保留「重新执行」按钮作为兜底（无 session、会话失效、非成功任务）；「继续执行」为唯一主按钮。
