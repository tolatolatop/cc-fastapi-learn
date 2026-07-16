import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  Ban,
  Bot,
  Check,
  ChevronRight,
  CircleAlert,
  Clock3,
  Copy,
  Ellipsis,
  KeyRound,
  Layers3,
  ListFilter,
  Menu,
  Plus,
  Radio,
  RefreshCw,
  Search,
  Server,
  Settings,
  SquareTerminal,
  Webhook,
  X,
} from 'lucide-react'
import { api } from './api'
import type { CreateTaskPayload, QueueItem, TaskContext, TaskItem, TaskLog, TaskStatus } from './types'
import WebhookPage from './WebhookPage'

const STATUS_META: Record<TaskStatus, { label: string; short: string }> = {
  queued: { label: '等待中', short: '等待' },
  running: { label: '执行中', short: '执行' },
  succeeded: { label: '已完成', short: '完成' },
  failed: { label: '失败', short: '失败' },
  cancelled: { label: '已取消', short: '取消' },
  abandoned: { label: '已中止', short: '中止' },
}

const FILTERS: Array<{ value: TaskStatus | 'all'; label: string }> = [
  { value: 'all', label: '全部任务' },
  { value: 'running', label: '执行中' },
  { value: 'queued', label: '等待中' },
  { value: 'succeeded', label: '已完成' },
  { value: 'failed', label: '异常' },
]

function shortId(id: string) {
  return id.slice(0, 8)
}

function formatDate(value: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function formatTime(value: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function formatDuration(start: string | null, end: string | null, now = Date.now()) {
  if (!start) return '—'
  const seconds = Math.max(0, Math.floor(((end ? new Date(end).getTime() : now) - new Date(start).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}

function messageFrom(error: unknown) {
  return error instanceof Error ? error.message : '发生未知错误'
}

function StatusBadge({ status }: { status: TaskStatus }) {
  return (
    <span className={`status-badge status-${status}`}>
      <i aria-hidden="true" />
      {STATUS_META[status].label}
    </span>
  )
}

function LogoMark() {
  return (
    <div className="logo-mark" aria-hidden="true">
      <span />
      <span />
      <span />
    </div>
  )
}

interface CreateTaskModalProps {
  queues: QueueItem[]
  onClose: () => void
  onCreated: (id: string) => void
}

function CreateTaskModal({ queues, onClose, onCreated }: CreateTaskModalProps) {
  const defaultQueue = queues.find((queue) => queue.is_default)?.name || queues[0]?.name || 'default'
  const [prompt, setPrompt] = useState('')
  const [queueName, setQueueName] = useState(defaultQueue)
  const [model, setModel] = useState('')
  const [priority, setPriority] = useState(0)
  const [maxAttempts, setMaxAttempts] = useState(3)
  const [source, setSource] = useState('console')
  const [advanced, setAdvanced] = useState(false)
  const [agentMode, setAgentMode] = useState(true)
  const [unattended, setUnattended] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, submitting])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!prompt.trim()) return
    setSubmitting(true)
    setError('')
    const payload: CreateTaskPayload = {
      prompt: prompt.trim(),
      queue_name: queueName,
      priority,
      max_attempts: maxAttempts,
      agent_mode: agentMode,
      unattended,
      ...(model.trim() ? { model: model.trim() } : {}),
      ...(source.trim() ? { metadata: { source: source.trim() } } : {}),
    }
    try {
      const result = await api.createTask(payload)
      onCreated(result.task_id)
    } catch (requestError) {
      setError(messageFrom(requestError))
      setSubmitting(false)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="create-modal" role="dialog" aria-modal="true" aria-labelledby="create-title">
        <div className="modal-head">
          <div>
            <p className="eyebrow">NEW DISPATCH</p>
            <h2 id="create-title">下发一个新任务</h2>
            <p>描述目标，任务将进入选定队列并由空闲 worker 接管。</p>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭" disabled={submitting}>
            <X size={19} />
          </button>
        </div>

        <form onSubmit={submit}>
          <label className="field prompt-field">
            <span>任务指令</span>
            <textarea
              autoFocus
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="例如：检查支付服务最近的错误日志，定位频繁超时的原因并给出修复建议…"
              rows={7}
              required
            />
            <small>{prompt.length} 字符</small>
          </label>

          <div className="form-grid">
            <label className="field">
              <span>目标队列</span>
              <select value={queueName} onChange={(event) => setQueueName(event.target.value)}>
                {queues.length ? (
                  queues.map((queue) => (
                    <option key={queue.name} value={queue.name}>
                      {queue.name}{queue.is_default ? ' · 默认' : ''}
                    </option>
                  ))
                ) : (
                  <option value="default">default · 默认</option>
                )}
              </select>
            </label>
            <label className="field">
              <span>优先级</span>
              <select value={priority} onChange={(event) => setPriority(Number(event.target.value))}>
                <option value={10}>高 · 优先处理</option>
                <option value={0}>标准</option>
                <option value={-10}>低 · 空闲处理</option>
              </select>
            </label>
          </div>

          <button className="advanced-toggle" type="button" onClick={() => setAdvanced((value) => !value)}>
            <Settings size={15} />
            高级选项
            <ChevronRight size={16} className={advanced ? 'rotated' : ''} />
          </button>

          {advanced && (
            <div className="advanced-panel">
              <div className="form-grid">
                <label className="field">
                  <span>模型覆盖</span>
                  <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="留空使用服务默认模型" />
                </label>
                <label className="field">
                  <span>最多尝试</span>
                  <input
                    type="number"
                    min={1}
                    max={20}
                    value={maxAttempts}
                    onChange={(event) => setMaxAttempts(Number(event.target.value))}
                  />
                </label>
                <label className="field">
                  <span>来源标签</span>
                  <input value={source} onChange={(event) => setSource(event.target.value)} placeholder="console" />
                </label>
              </div>
              <div className="switch-row">
                <button
                  type="button"
                  className={`switch ${agentMode ? 'is-on' : ''}`}
                  role="switch"
                  aria-checked={agentMode}
                  onClick={() => setAgentMode((value) => !value)}
                >
                  <span />
                </button>
                <div><strong>Agent 模式</strong><small>允许模型自主调用工具完成目标</small></div>
              </div>
              <div className="switch-row">
                <button
                  type="button"
                  className={`switch ${unattended ? 'is-on' : ''}`}
                  role="switch"
                  aria-checked={unattended}
                  onClick={() => setUnattended((value) => !value)}
                >
                  <span />
                </button>
                <div><strong>无人值守</strong><small>无需中途确认，持续运行到任务结束</small></div>
              </div>
            </div>
          )}

          {error && <div className="inline-error"><CircleAlert size={16} />{error}</div>}

          <div className="modal-actions">
            <button type="button" className="button button-quiet" onClick={onClose} disabled={submitting}>取消</button>
            <button type="submit" className="button button-primary" disabled={!prompt.trim() || submitting}>
              {submitting ? <RefreshCw className="spin" size={17} /> : <Plus size={18} />}
              {submitting ? '正在下发' : '下发任务'}
            </button>
          </div>
        </form>
      </section>
    </div>
  )
}

interface SettingsModalProps {
  onClose: () => void
  onSaved: () => void
}

function SettingsModal({ onClose, onSaved }: SettingsModalProps) {
  const [token, setToken] = useState(() => localStorage.getItem('cc-api-token') || '')

  function save(event: FormEvent) {
    event.preventDefault()
    if (token.trim()) localStorage.setItem('cc-api-token', token.trim())
    else localStorage.removeItem('cc-api-token')
    onSaved()
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <div className="modal-head compact">
          <div>
            <p className="eyebrow">CONNECTION</p>
            <h2 id="settings-title">连接设置</h2>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭"><X size={19} /></button>
        </div>
        <form onSubmit={save}>
          <label className="field">
            <span>API Token</span>
            <div className="input-with-icon">
              <KeyRound size={16} />
              <input type="password" value={token} onChange={(event) => setToken(event.target.value)} placeholder="未启用鉴权时可留空" />
            </div>
            <small>仅保存在当前浏览器的本地存储中。</small>
          </label>
          <div className="modal-actions">
            <button type="button" className="button button-quiet" onClick={onClose}>取消</button>
            <button type="submit" className="button button-primary"><Check size={17} />保存并重连</button>
          </div>
        </form>
      </section>
    </div>
  )
}

interface DetailDrawerProps {
  task: TaskItem
  logs: TaskLog[]
  context: TaskContext | null
  loading: boolean
  now: number
  onClose: () => void
  onCancel: (id: string) => void
  onRetry: (id: string) => void
  onRefresh: () => void
  retrying: boolean
}

function DetailDrawer({ task, logs, context, loading, now, onClose, onCancel, onRetry, onRefresh, retrying }: DetailDrawerProps) {
  const [tab, setTab] = useState<'overview' | 'context' | 'logs'>('overview')
  const canCancel = task.status === 'running' || task.status === 'queued'
  const canRetry = ['succeeded', 'failed', 'cancelled', 'abandoned'].includes(task.status)
  const copyId = () => navigator.clipboard?.writeText(task.id)

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="drawer-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside className="detail-drawer" role="dialog" aria-modal="true" aria-labelledby="detail-title">
        <div className="drawer-head">
          <button className="icon-button" onClick={onClose} aria-label="关闭详情"><X size={19} /></button>
          <div className="drawer-head-actions">
            <button className="icon-button" onClick={onRefresh} aria-label="刷新任务详情">
              <RefreshCw size={17} className={loading ? 'spin' : ''} />
            </button>
            <button className="icon-button" aria-label="更多操作"><Ellipsis size={19} /></button>
          </div>
        </div>

        <div className="drawer-intro">
          <div className="drawer-kicker">
            <StatusBadge status={task.status} />
            <span>{task.queue_name}</span>
          </div>
          <h2 id="detail-title">{task.prompt || '未命名任务'}</h2>
          <button className="copy-id" onClick={copyId} title="复制完整任务 ID">
            TASK-{shortId(task.id).toUpperCase()} <Copy size={13} />
          </button>
        </div>

        <div className="drawer-tabs" role="tablist">
          <button className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}>概览</button>
          <button className={tab === 'context' ? 'active' : ''} onClick={() => setTab('context')}>实时输出 <span>{context?.messages.length || 0}</span></button>
          <button className={tab === 'logs' ? 'active' : ''} onClick={() => setTab('logs')}>事件 <span>{logs.length}</span></button>
        </div>

        <div className="drawer-body">
          {tab === 'overview' && (
            <>
              <section className="detail-section">
                <h3>运行信息</h3>
                <dl className="detail-grid">
                  <div><dt>创建时间</dt><dd>{formatDate(task.created_at)}</dd></div>
                  <div><dt>运行时长</dt><dd>{formatDuration(task.started_at, task.finished_at, now)}</dd></div>
                  <div><dt>执行进度</dt><dd>{task.attempt} / {task.max_attempts} 次</dd></div>
                  <div><dt>优先级</dt><dd>{task.priority > 0 ? `高 · ${task.priority}` : task.priority < 0 ? `低 · ${task.priority}` : '标准 · 0'}</dd></div>
                  <div className="wide"><dt>模型</dt><dd className="mono-wrap">{task.model || '服务默认模型'}</dd></div>
                </dl>
              </section>

              {task.error_message && (
                <section className="error-card">
                  <CircleAlert size={18} />
                  <div><strong>执行异常</strong><p>{task.error_message}</p></div>
                </section>
              )}

              {task.abandoned_reason && (
                <section className="error-card warning">
                  <Ban size={18} />
                  <div><strong>中止原因</strong><p>{task.abandoned_reason}</p></div>
                </section>
              )}

              <section className="detail-section">
                <h3>执行方式</h3>
                <div className="flag-list">
                  <div><Bot size={17} /><span>Agent 模式</span><strong>{task.agent_mode ? '已启用' : '已关闭'}</strong></div>
                  <div><Radio size={17} /><span>无人值守</span><strong>{task.unattended ? '已启用' : '已关闭'}</strong></div>
                </div>
              </section>

              {task.metadata && Object.keys(task.metadata).length > 0 && (
                <section className="detail-section">
                  <h3>任务元数据</h3>
                  <pre className="code-block">{JSON.stringify(task.metadata, null, 2)}</pre>
                </section>
              )}

              {task.result && (
                <section className="detail-section">
                  <h3>任务结果</h3>
                  <pre className="code-block result-block">{JSON.stringify(task.result, null, 2)}</pre>
                </section>
              )}
            </>
          )}

          {tab === 'context' && (
            <section className="stream-panel">
              <div className="stream-head">
                <span><i className={task.status === 'running' ? 'live' : ''} />Agent 输出</span>
                <small>{context?.updated_at ? `更新于 ${formatTime(context.updated_at)}` : '暂无更新'}</small>
              </div>
              {context?.messages.length ? (
                <div className="stream-messages">
                  {context.messages.map((message, index) => <pre key={`${index}-${message.slice(0, 12)}`}>{message}</pre>)}
                </div>
              ) : (
                <div className="detail-empty"><SquareTerminal size={26} /><strong>还没有实时输出</strong><p>任务开始执行后，Agent 的消息会显示在这里。</p></div>
              )}
            </section>
          )}

          {tab === 'logs' && (
            <section className="timeline">
              {logs.length ? logs.map((log) => (
                <div className={`timeline-item level-${log.level.toLowerCase()}`} key={log.id}>
                  <i />
                  <div className="timeline-content">
                    <div><strong>{log.event_type}</strong><time>{formatTime(log.ts)}</time></div>
                    <p>{log.message}</p>
                    {log.metadata && <pre>{JSON.stringify(log.metadata, null, 2)}</pre>}
                  </div>
                </div>
              )) : <div className="detail-empty"><Activity size={26} /><strong>暂无事件</strong><p>任务状态变化会记录在这里。</p></div>}
            </section>
          )}
        </div>

        {(canCancel || canRetry) && (
          <div className="drawer-footer">
            {canCancel && <button className="button button-danger" onClick={() => onCancel(task.id)}><Ban size={16} />取消任务</button>}
            {canRetry && (
              <button className="button button-retry" onClick={() => onRetry(task.id)} disabled={retrying}>
                <RefreshCw size={16} className={retrying ? 'spin' : ''} />
                {retrying ? '正在创建' : '重新执行'}
              </button>
            )}
            <p>{canRetry ? '将复制当前配置，并创建一个新的排队任务。' : '已开始的 Agent 操作可能需要短暂时间才能停止。'}</p>
          </div>
        )}
      </aside>
    </div>
  )
}

function App() {
  const [activeView, setActiveView] = useState<'tasks' | 'webhooks'>(() => window.location.hash === '#/webhooks' ? 'webhooks' : 'tasks')
  const [tasks, setTasks] = useState<TaskItem[]>([])
  const [total, setTotal] = useState(0)
  const [queues, setQueues] = useState<QueueItem[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [apiOnline, setApiOnline] = useState<boolean | null>(null)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'all'>('all')
  const [queueFilter, setQueueFilter] = useState('all')
  const [selectedTask, setSelectedTask] = useState<TaskItem | null>(null)
  const [logs, setLogs] = useState<TaskLog[]>([])
  const [context, setContext] = useState<TaskContext | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [retryingTask, setRetryingTask] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [connectionRevision, setConnectionRevision] = useState(0)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [toast, setToast] = useState('')
  const [now, setNow] = useState(Date.now())

  const loadDashboard = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true)
    else setLoading(true)
    try {
      const [taskResponse, queueResponse] = await Promise.all([api.listTasks(), api.listQueues()])
      setTasks(taskResponse.items)
      setTotal(taskResponse.total)
      setQueues(queueResponse.items)
      setError('')
      setApiOnline(true)
      setSelectedTask((current) => current ? taskResponse.items.find((item) => item.id === current.id) || current : null)
    } catch (requestError) {
      setError(messageFrom(requestError))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  const loadDetail = useCallback(async (id: string, showLoader = true) => {
    if (showLoader) setDetailLoading(true)
    try {
      const [task, logResponse, contextResponse] = await Promise.all([api.getTask(id), api.getLogs(id), api.getContext(id)])
      setSelectedTask(task)
      setLogs(logResponse.items)
      setContext(contextResponse)
    } catch (requestError) {
      setToast(messageFrom(requestError))
    } finally {
      setDetailLoading(false)
    }
  }, [])

  const checkHealth = useCallback(async () => {
    try {
      await api.health()
      setApiOnline(true)
    } catch {
      setApiOnline(false)
    }
  }, [])

  useEffect(() => {
    loadDashboard()
    checkHealth()
    const refreshTimer = window.setInterval(() => {
      if (!document.hidden) loadDashboard(true)
    }, 5000)
    const healthTimer = window.setInterval(checkHealth, 15000)
    const clockTimer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => {
      window.clearInterval(refreshTimer)
      window.clearInterval(healthTimer)
      window.clearInterval(clockTimer)
    }
  }, [checkHealth, loadDashboard])

  useEffect(() => {
    if (!selectedTask) return
    const detailTimer = window.setInterval(() => {
      if (!document.hidden) loadDetail(selectedTask.id, false)
    }, 3500)
    return () => window.clearInterval(detailTimer)
  }, [loadDetail, selectedTask?.id])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(''), 3500)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    const onHashChange = () => setActiveView(window.location.hash === '#/webhooks' ? 'webhooks' : 'tasks')
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const counts = useMemo(() => {
    const result = { all: tasks.length, queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0, abandoned: 0 }
    tasks.forEach((task) => { result[task.status] += 1 })
    return result
  }, [tasks])

  const visibleTasks = useMemo(() => {
    const query = search.trim().toLowerCase()
    return tasks.filter((task) => {
      const matchesStatus = statusFilter === 'all' || task.status === statusFilter || (statusFilter === 'failed' && task.status === 'abandoned')
      const matchesQueue = queueFilter === 'all' || task.queue_name === queueFilter
      const matchesSearch = !query || task.prompt.toLowerCase().includes(query) || task.id.toLowerCase().includes(query) || task.queue_name.toLowerCase().includes(query)
      return matchesStatus && matchesQueue && matchesSearch
    })
  }, [queueFilter, search, statusFilter, tasks])

  const displayQueues = useMemo(() => {
    if (queues.length) return queues
    const names = [...new Set(tasks.map((task) => task.queue_name))]
    return names.map((name, index) => ({ name, workers: 0, is_default: index === 0 }))
  }, [queues, tasks])

  const taskStatuses = useMemo(
    () => Object.fromEntries(tasks.map((task) => [task.id, task.status])) as Record<string, TaskStatus>,
    [tasks],
  )

  async function openTask(task: TaskItem) {
    setSelectedTask(task)
    setLogs([])
    setContext(null)
    await loadDetail(task.id)
  }

  async function created(id: string) {
    setCreateOpen(false)
    setToast('任务已进入队列')
    await loadDashboard(true)
    await loadDetail(id)
  }

  async function cancelTask(id: string) {
    if (!window.confirm('确认取消这个任务？')) return
    try {
      await api.cancelTask(id)
      setToast('任务已取消')
      await Promise.all([loadDashboard(true), loadDetail(id, false)])
    } catch (requestError) {
      setToast(messageFrom(requestError))
    }
  }

  async function retryTask(id: string) {
    if (!window.confirm('将使用相同配置创建一个新任务，确认重新执行？')) return
    setRetryingTask(true)
    try {
      const result = await api.retryTask(id)
      setToast('已创建新的重试任务')
      await loadDashboard(true)
      await loadDetail(result.task_id)
    } catch (requestError) {
      setToast(messageFrom(requestError))
    } finally {
      setRetryingTask(false)
    }
  }

  function handleRowKey(event: KeyboardEvent<HTMLTableRowElement>, task: TaskItem) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      openTask(task)
    }
  }

  function navigate(view: 'tasks' | 'webhooks') {
    setActiveView(view)
    window.location.hash = view === 'webhooks' ? '/webhooks' : '/tasks'
    setSidebarOpen(false)
  }

  function showQueueRail() {
    navigate('tasks')
    window.setTimeout(() => document.getElementById('queue-rail')?.scrollIntoView({ behavior: 'smooth' }), 0)
  }

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
        <div className="brand">
          <LogoMark />
          <div><strong>Agent Queue</strong><span>CONTROL ROOM</span></div>
          <button className="sidebar-close" onClick={() => setSidebarOpen(false)} aria-label="关闭导航"><X size={19} /></button>
        </div>

        <nav aria-label="主导航">
          <p>工作区</p>
          <button className={`nav-item ${activeView === 'tasks' ? 'active' : ''}`} onClick={() => navigate('tasks')}><Layers3 size={18} /><span>任务调度</span><b>{total}</b></button>
          <button className={`nav-item ${activeView === 'webhooks' ? 'active' : ''}`} onClick={() => navigate('webhooks')}>
            <Webhook size={18} /><span>Webhook 档案</span>
          </button>
          <button className="nav-item" onClick={showQueueRail}>
            <Activity size={18} /><span>队列状态</span>
          </button>
          <button className="nav-item" onClick={() => setSettingsOpen(true)}><Settings size={18} /><span>连接设置</span></button>
        </nav>

        <div className="sidebar-queues">
          <p>活跃队列</p>
          {displayQueues.map((queue) => {
            const active = tasks.filter((task) => task.queue_name === queue.name && task.status === 'running').length
            return (
              <button key={queue.name} onClick={() => { setQueueFilter(queue.name); setSidebarOpen(false) }}>
                <i className={active ? 'busy' : ''} />
                <span>{queue.name}</span>
                <small>{active}/{queue.workers || '—'}</small>
              </button>
            )
          })}
        </div>

        <div className="sidebar-status">
          <div className={`service-dot ${apiOnline === false ? 'offline' : ''}`}><span /></div>
          <div><strong>{apiOnline === false ? '服务离线' : apiOnline === null ? '正在连接' : '服务运行正常'}</strong><span>API · {apiOnline === false ? 'unreachable' : 'connected'}</span></div>
        </div>
      </aside>

      {sidebarOpen && <button className="sidebar-scrim" onClick={() => setSidebarOpen(false)} aria-label="关闭导航" />}

      <main>
        <header className="topbar">
          <button className="mobile-menu" onClick={() => setSidebarOpen(true)} aria-label="打开导航"><Menu size={20} /></button>
          <div className="breadcrumb"><span>控制台</span><ChevronRight size={14} /><strong>{activeView === 'tasks' ? '任务调度' : 'Webhook 档案'}</strong></div>
          <div className="top-actions">
            <span className="last-sync"><RefreshCw size={13} className={activeView === 'tasks' && refreshing ? 'spin' : ''} />{activeView === 'tasks' ? '5 秒自动同步' : '10 秒自动同步'}</span>
            <button className="icon-button" onClick={() => setSettingsOpen(true)} aria-label="连接设置"><Settings size={18} /></button>
            {activeView === 'tasks' && <button className="button button-primary top-create" onClick={() => setCreateOpen(true)}><Plus size={18} />新建任务</button>}
          </div>
        </header>

        <div className={`workspace ${activeView === 'webhooks' ? 'webhook-workspace' : ''}`}>
          {activeView === 'tasks' ? (
          <>
          <section className="page-heading">
            <div>
              <p className="eyebrow">AGENT OPERATIONS / 实时调度</p>
              <h1>任务编排台</h1>
              <p>下发任务，观察队列，定位每一次异常。</p>
            </div>
            <button className="button mobile-create" onClick={() => setCreateOpen(true)}><Plus size={18} />新建任务</button>
          </section>

          <section className="queue-rail" id="queue-rail" aria-label="队列实时状态">
            <div className="rail-summary">
              <span className="live-label"><i />LIVE</span>
              <div><strong>{counts.running}</strong><span>正在执行</span></div>
              <div><strong>{counts.queued}</strong><span>等待接管</span></div>
            </div>
            <div className="rail-track">
              <div className="track-line" />
              {displayQueues.map((queue) => {
                const active = tasks.filter((task) => task.queue_name === queue.name && task.status === 'running').length
                const waiting = tasks.filter((task) => task.queue_name === queue.name && task.status === 'queued').length
                return (
                  <button
                    className={`rail-node ${active ? 'active' : ''}`}
                    key={queue.name}
                    onClick={() => setQueueFilter(queue.name)}
                    title={`筛选 ${queue.name} 队列`}
                  >
                    <span className="node-dot"><i /></span>
                    <strong>{queue.name}</strong>
                    <small>{active} 执行 · {waiting} 等待</small>
                  </button>
                )
              })}
            </div>
            <div className="rail-clock">
              <strong>{new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(now)}</strong>
              <span>LOCAL / CST</span>
            </div>
          </section>

          <section className="task-panel">
            <div className="panel-summary">
              <div><span>任务总数</span><strong>{total}</strong><small>最近载入 {tasks.length} 条</small></div>
              <div><span>执行中</span><strong className="blue">{counts.running}</strong><small>{counts.queued} 条等待中</small></div>
              <div><span>已完成</span><strong>{counts.succeeded}</strong><small>{tasks.length ? Math.round((counts.succeeded / tasks.length) * 100) : 0}% 当前成功率</small></div>
              <div><span>需关注</span><strong className={counts.failed + counts.abandoned ? 'coral' : ''}>{counts.failed + counts.abandoned}</strong><small>失败与中止</small></div>
            </div>

            <div className="panel-toolbar">
              <div className="filter-tabs">
                {FILTERS.map((filter) => (
                  <button
                    key={filter.value}
                    className={statusFilter === filter.value ? 'active' : ''}
                    onClick={() => setStatusFilter(filter.value)}
                  >
                    {filter.label}
                    <span>{filter.value === 'all' ? counts.all : filter.value === 'failed' ? counts.failed + counts.abandoned : counts[filter.value]}</span>
                  </button>
                ))}
              </div>
              <div className="toolbar-tools">
                <label className="search-box">
                  <Search size={16} />
                  <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索指令或 ID" aria-label="搜索任务" />
                  {search && <button onClick={() => setSearch('')} aria-label="清除搜索"><X size={14} /></button>}
                </label>
                <label className="queue-select">
                  <ListFilter size={16} />
                  <select value={queueFilter} onChange={(event) => setQueueFilter(event.target.value)} aria-label="筛选队列">
                    <option value="all">全部队列</option>
                    {displayQueues.map((queue) => <option key={queue.name} value={queue.name}>{queue.name}</option>)}
                  </select>
                </label>
                <button className="icon-button toolbar-refresh" onClick={() => loadDashboard(true)} aria-label="立即刷新"><RefreshCw size={17} className={refreshing ? 'spin' : ''} /></button>
              </div>
            </div>

            <div className="task-list-wrap">
              {loading ? (
                <div className="state-message"><RefreshCw size={24} className="spin" /><strong>正在连接任务队列</strong><p>读取当前任务和 worker 状态…</p></div>
              ) : error ? (
                <div className="state-message error-state">
                  <CircleAlert size={26} />
                  <strong>无法读取任务</strong>
                  <p>{error}</p>
                  <div>
                    {error === 'invalid api token' && <button className="button button-quiet" onClick={() => setSettingsOpen(true)}><KeyRound size={16} />填写 Token</button>}
                    <button className="button button-primary" onClick={() => loadDashboard()}><RefreshCw size={16} />重试连接</button>
                  </div>
                </div>
              ) : visibleTasks.length === 0 ? (
                <div className="state-message">
                  <Layers3 size={26} />
                  <strong>{tasks.length ? '没有符合条件的任务' : '队列还是空的'}</strong>
                  <p>{tasks.length ? '调整状态、队列或搜索条件后再试。' : '下发第一个任务，worker 会自动接管执行。'}</p>
                  {!tasks.length && <button className="button button-primary" onClick={() => setCreateOpen(true)}><Plus size={17} />新建任务</button>}
                </div>
              ) : (
                <table className="task-table">
                  <thead>
                    <tr><th>任务</th><th>状态</th><th>队列</th><th>创建时间</th><th>耗时</th><th>尝试</th><th><span className="sr-only">操作</span></th></tr>
                  </thead>
                  <tbody>
                    {visibleTasks.map((task) => (
                      <tr key={task.id} tabIndex={0} onClick={() => openTask(task)} onKeyDown={(event) => handleRowKey(event, task)}>
                        <td>
                          <div className="task-name"><strong>{task.prompt || '未命名任务'}</strong><span>TASK-{shortId(task.id).toUpperCase()} · {task.model || '默认模型'}</span></div>
                        </td>
                        <td><StatusBadge status={task.status} /></td>
                        <td><span className="queue-chip"><i />{task.queue_name}</span></td>
                        <td><span className="date-cell">{formatDate(task.created_at)}</span></td>
                        <td><span className="duration-cell">{formatDuration(task.started_at, task.finished_at, now)}</span></td>
                        <td><span className="attempt-cell">{task.attempt}<i>/</i>{task.max_attempts}</span></td>
                        <td><button className="row-action" onClick={(event) => { event.stopPropagation(); openTask(task) }} aria-label={`查看任务 ${shortId(task.id)}`}><ChevronRight size={17} /></button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {!loading && !error && visibleTasks.length > 0 && (
              <div className="panel-footer"><span>显示 {visibleTasks.length} / {total} 条任务</span><span><Server size={13} />数据来自实时 API</span></div>
            )}
          </section>
          </>
          ) : (
            <WebhookPage key={connectionRevision} taskStatuses={taskStatuses} onOpenTask={(taskId) => loadDetail(taskId)} onOpenSettings={() => setSettingsOpen(true)} />
          )}
        </div>
      </main>

      {createOpen && <CreateTaskModal queues={displayQueues} onClose={() => setCreateOpen(false)} onCreated={created} />}
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} onSaved={() => { setSettingsOpen(false); setConnectionRevision((value) => value + 1); loadDashboard() }} />}
      {selectedTask && (
        <DetailDrawer
          task={selectedTask}
          logs={logs}
          context={context}
          loading={detailLoading}
          now={now}
          onClose={() => setSelectedTask(null)}
          onCancel={cancelTask}
          onRetry={retryTask}
          onRefresh={() => loadDetail(selectedTask.id)}
          retrying={retryingTask}
        />
      )}
      {toast && <div className="toast"><Check size={16} />{toast}</div>}
    </div>
  )
}

export default App
