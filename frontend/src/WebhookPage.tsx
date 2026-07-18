import { KeyboardEvent, useCallback, useEffect, useState } from 'react'
import {
  ArrowRight,
  Braces,
  ChevronRight,
  CircleAlert,
  Copy,
  ExternalLink,
  GitBranch,
  Github,
  KeyRound,
  Layers3,
  RefreshCw,
  Search,
  Server,
  UserRound,
  Webhook,
  X,
} from 'lucide-react'
import { Button, Form, Offcanvas, Table } from 'react-bootstrap'
import { api } from './api'
import Pagination from './Pagination'
import type { TaskStatus, WebhookTrigger } from './types'

const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  queued: '等待中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  abandoned: '已中止',
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '发生未知错误'
}

function formatWebhookDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function formatWebhookTime(value: string) {
  const date = new Date(value)
  return {
    date: new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(date),
    time: new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date),
  }
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function stringValue(value: unknown) {
  return typeof value === 'string' ? value : ''
}

function payloadSummary(payload: Record<string, unknown>, eventType = 'event') {
  const project = objectValue(payload.project)
  const repository = objectValue(payload.repository)
  const attributes = objectValue(payload.object_attributes)
  const user = objectValue(payload.user)
  const sender = objectValue(payload.sender)
  const pullRequest = objectValue(payload.pull_request)
  const pullRequestHead = objectValue(pullRequest?.head)
  const projectName = stringValue(project?.path_with_namespace)
    || stringValue(project?.name)
    || stringValue(repository?.full_name)
    || stringValue(repository?.name)
    || '未提供项目'
  const rawRef = stringValue(payload.ref)
    || stringValue(attributes?.source_branch)
    || stringValue(attributes?.ref)
    || stringValue(pullRequestHead?.ref)
  const ref = rawRef.replace(/^refs\/(heads|tags)\//, '') || '—'
  const actor = stringValue(payload.user_name)
    || stringValue(payload.user_username)
    || stringValue(user?.name)
    || stringValue(user?.username)
    || stringValue(sender?.login)
    || '—'
  const kind = stringValue(payload.object_kind) || stringValue(payload.event_name) || eventType
  return { projectName, ref, actor, kind }
}

function eventTone(eventType: string) {
  const normalized = eventType.toLowerCase()
  if (normalized.includes('merge') || normalized.includes('pull')) return 'merge'
  if (normalized.includes('pipeline') || normalized.includes('job')) return 'pipeline'
  if (normalized.includes('tag')) return 'tag'
  return 'push'
}

function compactId(value: string | null) {
  if (!value) return '—'
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value
}

interface WebhookDetailProps {
  record: WebhookTrigger
  taskStatus?: TaskStatus
  onClose: () => void
  onOpenTask: (taskId: string) => void
}

function WebhookDetail({ record, taskStatus, onClose, onOpenTask }: WebhookDetailProps) {
  const summary = payloadSummary(record.payload, record.event_type)

  return (
    <Offcanvas show onHide={onClose} placement="end" className="detail-drawer webhook-detail-drawer" aria-labelledby="webhook-detail-title">
        <div className="drawer-head">
          <button className="icon-button" onClick={onClose} aria-label="关闭 Webhook 详情"><X size={19} /></button>
          <span className="drawer-record-id">HOOK-{String(record.id).padStart(5, '0')}</span>
        </div>

        <div className="webhook-detail-intro">
          <div className="webhook-provider"><span><i />{record.provider}</span><small>{record.event_type}</small></div>
          <h2 id="webhook-detail-title">{summary.projectName}</h2>
          <p><GitBranch size={14} />{summary.ref}</p>
        </div>

        <div className="webhook-detail-body">
          <section className="detail-section">
            <h3>触发信息</h3>
            <dl className="detail-grid webhook-meta-grid">
              <div><dt>接收时间</dt><dd>{formatWebhookDate(record.created_at)}</dd></div>
              <div><dt>事件类型</dt><dd>{record.event_type}</dd></div>
              <div><dt>触发用户</dt><dd>{summary.actor}</dd></div>
              <div><dt>对象类型</dt><dd>{summary.kind}</dd></div>
              <div className="wide"><dt>平台实例</dt><dd className="mono-wrap">{record.instance_url || '—'}</dd></div>
            </dl>
          </section>

          <section className="detail-section webhook-identifiers">
            <h3>关联标识</h3>
            <div>
              <span>Event UUID</span>
              <code>{record.event_uuid || '—'}</code>
              {record.event_uuid && <button onClick={() => navigator.clipboard?.writeText(record.event_uuid!)} aria-label="复制 Event UUID"><Copy size={14} /></button>}
            </div>
            <div>
              <span>Webhook UUID</span>
              <code>{record.webhook_uuid || '—'}</code>
              {record.webhook_uuid && <button onClick={() => navigator.clipboard?.writeText(record.webhook_uuid!)} aria-label="复制 Webhook UUID"><Copy size={14} /></button>}
            </div>
          </section>

          {record.task_id ? (
            <section className="linked-task-card">
              <div className="linked-task-icon"><Layers3 size={18} /></div>
              <div><span>关联 Agent 任务{taskStatus ? ` · ${TASK_STATUS_LABEL[taskStatus]}` : ''}</span><strong>TASK-{record.task_id.slice(0, 8).toUpperCase()}</strong></div>
              <Button variant="primary" onClick={() => onOpenTask(record.task_id!)}>查看任务<ExternalLink size={14} /></Button>
            </section>
          ) : (
            <section className="workflow-skip-card">
              <CircleAlert size={18} />
              <div><span>工作流未创建任务</span><strong>{record.skip_reason || '事件已被前置规则跳过'}</strong></div>
            </section>
          )}

          <section className="detail-section payload-section">
            <div className="payload-heading"><h3>原始 Payload</h3><span><Braces size={13} />JSON</span></div>
            <pre className="code-block webhook-payload">{JSON.stringify(record.payload, null, 2)}</pre>
          </section>
        </div>
    </Offcanvas>
  )
}

interface WebhookPageProps {
  onOpenTask: (taskId: string) => void
  onOpenSettings: () => void
}

export default function WebhookPage({ onOpenTask, onOpenSettings }: WebhookPageProps) {
  const [records, setRecords] = useState<WebhookTrigger[]>([])
  const [total, setTotal] = useState(0)
  const [summaryTotal, setSummaryTotal] = useState(0)
  const [eventTypes, setEventTypes] = useState<string[]>([])
  const [providers, setProviders] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [eventFilter, setEventFilter] = useState('all')
  const [providerFilter, setProviderFilter] = useState('all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [selected, setSelected] = useState<WebhookTrigger | null>(null)

  const loadRecords = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true)
    else setLoading(true)
    try {
      const response = await api.listWebhooks({
        offset: (page - 1) * pageSize,
        limit: pageSize,
        eventType: eventFilter === 'all' ? undefined : eventFilter,
        provider: providerFilter === 'all' ? undefined : providerFilter,
        query: debouncedSearch.trim() || undefined,
      })
      setRecords(response.items)
      setTotal(response.total)
      setSummaryTotal(response.summary.total)
      setEventTypes(response.summary.event_types)
      setProviders(response.summary.providers)
      setError('')
      setSelected((current) => current ? response.items.find((item) => item.id === current.id) || current : null)
      const lastPage = Math.max(1, Math.ceil(response.total / pageSize))
      if (page > lastPage) setPage(lastPage)
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [debouncedSearch, eventFilter, page, pageSize, providerFilter])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search), 300)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    loadRecords()
    const timer = window.setInterval(() => {
      if (!document.hidden) loadRecords(true)
    }, 10000)
    return () => window.clearInterval(timer)
  }, [loadRecords])

  function handleRowKey(event: KeyboardEvent<HTMLTableRowElement>, record: WebhookTrigger) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      setSelected(record)
    }
  }

  return (
    <>
      <section className="page-heading webhook-page-heading">
        <div>
          <p className="eyebrow">INBOUND EVENTS / 触发检索</p>
          <h1>Webhook 档案</h1>
          <p>检索每次外部触发，追溯它创建的 Agent 任务。</p>
        </div>
        <Button variant="outline-secondary" className="webhook-refresh" onClick={() => loadRecords(true)}>
          <RefreshCw size={16} className={refreshing ? 'spin' : ''} />刷新记录
        </Button>
      </section>

      <section className="ingress-rail" aria-label="Webhook 处理流程">
        <div className="ingress-source">
          <div className="provider-glyphs" aria-hidden="true"><span>GL</span><span><Github size={14} /></span></div>
          <div><span>事件来源</span><strong>GitLab + GitHub</strong></div>
        </div>
        <div className="ingress-route">
          <div className="route-line"><i /><i /><i /></div>
          <span>POST /v1/webhooks/&#123;provider&#125;</span>
        </div>
        <div className="ingress-gateway">
          <div className="gateway-rings"><Webhook size={18} /></div>
          <div><span>接收记录</span><strong>{summaryTotal}</strong></div>
        </div>
        <ArrowRight className="ingress-arrow" size={18} />
        <div className="ingress-target">
          <Layers3 size={19} />
          <div><span>下游目标</span><strong>Agent Task</strong></div>
        </div>
        <div className="ingress-live"><i />10 秒同步</div>
      </section>

      <section className="webhook-panel">
        <div className="webhook-panel-head">
          <div>
            <p className="eyebrow">EVENT LEDGER</p>
            <h2>触发记录</h2>
            <span>共归档 {summaryTotal.toLocaleString('zh-CN')} 条，当前页 {records.length} 条</span>
          </div>
          <div className="webhook-search-tools">
            <label className="webhook-search">
              <Search size={18} />
              <Form.Control
                value={search}
                onChange={(event) => { setSearch(event.target.value); setPage(1) }}
                placeholder="搜索项目、分支、UUID 或任务 ID"
                aria-label="搜索 Webhook 记录"
              />
              {search && <button onClick={() => { setSearch(''); setPage(1) }} aria-label="清除搜索"><X size={15} /></button>}
            </label>
            <label className="webhook-event-filter">
              <Form.Select value={providerFilter} onChange={(event) => { setProviderFilter(event.target.value); setPage(1) }} aria-label="筛选 Webhook 平台">
                <option value="all">全部平台</option>
                {providers.map((provider) => <option key={provider} value={provider}>{provider === 'github' ? 'GitHub' : provider === 'gitlab' ? 'GitLab' : provider}</option>)}
              </Form.Select>
            </label>
            <label className="webhook-event-filter">
              <Form.Select value={eventFilter} onChange={(event) => { setEventFilter(event.target.value); setPage(1) }} aria-label="筛选事件类型">
                <option value="all">全部事件</option>
                {eventTypes.map((eventType) => <option key={eventType} value={eventType}>{eventType}</option>)}
              </Form.Select>
            </label>
          </div>
        </div>

        <div className="webhook-results-meta">
          <span>找到 <strong>{total.toLocaleString('zh-CN')}</strong> 条记录</span>
          {(search || eventFilter !== 'all' || providerFilter !== 'all') && <button onClick={() => { setSearch(''); setEventFilter('all'); setProviderFilter('all'); setPage(1) }}>清除检索条件</button>}
        </div>

        <div className="webhook-list-wrap">
          {loading ? (
            <div className="state-message"><RefreshCw size={24} className="spin" /><strong>正在读取 Webhook 档案</strong><p>加载触发记录和关联任务…</p></div>
          ) : error ? (
            <div className="state-message error-state">
              <CircleAlert size={26} /><strong>无法读取 Webhook 记录</strong><p>{error}</p>
              <div>
                {error === 'invalid api token' && <Button variant="outline-secondary" onClick={onOpenSettings}><KeyRound size={16} />填写 Token</Button>}
                <Button variant="primary" onClick={() => loadRecords()}><RefreshCw size={16} />重试连接</Button>
              </div>
            </div>
          ) : records.length === 0 ? (
            <div className="state-message webhook-empty">
              <Webhook size={27} />
              <strong>{summaryTotal ? '没有匹配的触发记录' : '还没有收到 Webhook'}</strong>
              <p>{summaryTotal ? '尝试搜索项目名、分支、UUID 或关联任务 ID。' : '在 GitLab 或 GitHub 中配置 Webhook 后，收到的事件会归档在这里。'}</p>
            </div>
          ) : (
            <Table hover className="webhook-table">
              <thead><tr><th>接收时间</th><th>事件</th><th>项目 / 分支</th><th>事件标识</th><th>关联任务</th><th><span className="sr-only">操作</span></th></tr></thead>
              <tbody>
                {records.map((record) => {
                  const summary = payloadSummary(record.payload, record.event_type)
                  const received = formatWebhookTime(record.created_at)
                  return (
                    <tr key={record.id} tabIndex={0} onClick={() => setSelected(record)} onKeyDown={(event) => handleRowKey(event, record)}>
                      <td><div className="webhook-time"><strong>{received.time}</strong><span>{received.date} · #{record.id}</span></div></td>
                      <td><div className="webhook-event-kind"><small>{record.provider}</small><span className={`event-chip event-${eventTone(record.event_type)}`}><i />{record.event_type}</span></div></td>
                      <td><div className="webhook-subject"><strong>{summary.projectName}</strong><span><GitBranch size={12} />{summary.ref}<i /> <UserRound size={11} />{summary.actor}</span></div></td>
                      <td><div className="webhook-ids"><code title={record.event_uuid || ''}>{compactId(record.event_uuid)}</code><span title={record.webhook_uuid || ''}>{compactId(record.webhook_uuid)}</span></div></td>
                      <td>
                        {record.task_id ? (
                          <button className="task-link" onClick={(event) => { event.stopPropagation(); onOpenTask(record.task_id!) }}>
                            {record.task_status && <i className={`task-dot task-${record.task_status}`} title={TASK_STATUS_LABEL[record.task_status]} />}
                            TASK-{record.task_id.slice(0, 8).toUpperCase()}<ExternalLink size={12} />
                          </button>
                        ) : (
                          <span className="workflow-decision workflow-skipped" title={record.skip_reason || ''}><i />已跳过</span>
                        )}
                      </td>
                      <td><button className="row-action" onClick={(event) => { event.stopPropagation(); setSelected(record) }} aria-label={`查看 Webhook ${record.id}`}><ChevronRight size={17} /></button></td>
                    </tr>
                  )
                })}
              </tbody>
            </Table>
          )}
        </div>

        {!loading && !error && records.length > 0 && (
          <div className="panel-footer">
            <Pagination
              page={page}
              pageSize={pageSize}
              total={total}
              itemLabel="Webhook 记录"
              onPageChange={setPage}
              onPageSizeChange={(value) => { setPageSize(value); setPage(1) }}
            />
            <span className="panel-source"><Server size={13} />数据来自 Webhook API</span>
          </div>
        )}
      </section>

      {selected && <WebhookDetail record={selected} taskStatus={selected.task_status || undefined} onClose={() => setSelected(null)} onOpenTask={onOpenTask} />}
    </>
  )
}
