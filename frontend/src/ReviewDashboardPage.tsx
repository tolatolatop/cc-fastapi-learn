import { KeyboardEvent, useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  ExternalLink,
  FileCode2,
  GitCommitHorizontal,
  GitPullRequest,
  KeyRound,
  ListChecks,
  RefreshCw,
  Server,
  ShieldCheck,
  X,
} from 'lucide-react'
import { Button, ButtonGroup, Form, Offcanvas, Table } from 'react-bootstrap'
import { api } from './api'
import Pagination from './Pagination'
import type {
  ReviewBatchStatus,
  ReviewDashboardOutcome,
  ReviewDashboardPullRequest,
  ReviewDashboardPullRequestDetail,
  ReviewDashboardResponse,
  ReviewDashboardTask,
  ReviewDashboardTrendPoint,
  ReviewIssue,
  ReviewIssueSeverity,
  ReviewIssueVerificationStatus,
  TaskStatus,
} from './types'

const TASK_STATUS_META: Record<TaskStatus, { label: string; short: string }> = {
  queued: { label: '排队中', short: '等待' },
  running: { label: '执行中', short: '执行' },
  succeeded: { label: '已成功', short: '成功' },
  failed: { label: '失败', short: '失败' },
  cancelled: { label: '已取消', short: '取消' },
  abandoned: { label: '已中止', short: '中止' },
}

const BATCH_STATUS_LABEL: Record<ReviewBatchStatus, string> = {
  collecting: '提取中',
  waiting_merge: '等待合入',
  verifying: '验证中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const ISSUE_STATUS_META: Record<ReviewIssueVerificationStatus, { label: string; tone: string }> = {
  accepted: { label: '已接受', tone: 'accepted' },
  not_accepted: { label: '合入未处理', tone: 'unhandled' },
  unverified: { label: '待确认', tone: 'pending' },
}

const SEVERITY_LABEL: Record<ReviewIssueSeverity, string> = {
  critical: '严重',
  high: '高',
  medium: '中',
  low: '低',
  info: '提示',
}

const ROLE_LABEL: Record<ReviewDashboardTask['role'], string> = {
  review: '检视',
  extract: '提取',
  verify: '验证',
}

const EMPTY_DASHBOARD: ReviewDashboardResponse = {
  summary: {
    pull_request_total: 0,
    batch_total: 0,
    issue_total: 0,
    accepted_issues: 0,
    merged_unhandled_issues: 0,
    pending_issues: 0,
    acceptance_rate: null,
  },
  timeline: [],
  repositories: [],
  tags: [],
  items: [],
  total: 0,
}

function messageFrom(error: unknown) {
  return error instanceof Error ? error.message : '发生未知错误'
}

function inputDate(value: Date) {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function daysAgo(days: number) {
  const value = new Date()
  value.setHours(0, 0, 0, 0)
  value.setDate(value.getDate() - days)
  return inputDate(value)
}

function apiDateBounds(from: string, to: string) {
  return {
    createdFrom: new Date(`${from}T00:00:00`).toISOString(),
    createdTo: new Date(`${to}T23:59:59.999`).toISOString(),
  }
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

function formatFullDate(value: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function compactTask(value: string) {
  return `TASK-${value.slice(0, 8).toUpperCase()}`
}

function repositoryKey(provider: string, projectPath: string) {
  return `${encodeURIComponent(provider)}|${encodeURIComponent(projectPath)}`
}

function parseRepositoryKey(value: string) {
  if (!value) return { provider: undefined, projectPath: undefined }
  const [provider, projectPath] = value.split('|')
  return {
    provider: decodeURIComponent(provider),
    projectPath: decodeURIComponent(projectPath),
  }
}

function TaskStatusCluster({ counts }: { counts: Record<TaskStatus, number> }) {
  const visible = (Object.entries(counts) as Array<[TaskStatus, number]>).filter(([, count]) => count > 0)
  return (
    <div className="review-task-cluster" aria-label="检查任务状态">
      {visible.map(([status, count]) => (
        <span className={`review-task-cluster-item task-${status}`} key={status} title={`${TASK_STATUS_META[status].label}：${count}`}>
          <i />{TASK_STATUS_META[status].short}<b>{count}</b>
        </span>
      ))}
    </div>
  )
}

function OutcomeMiniBar({ pullRequest }: { pullRequest: ReviewDashboardPullRequest }) {
  const denominator = Math.max(1, pullRequest.issue_total)
  return (
    <div className="review-outcome-mini" aria-label={`${pullRequest.issue_total} 个问题`}>
      <span className="accepted" style={{ width: `${pullRequest.accepted_issues / denominator * 100}%` }} />
      <span className="unhandled" style={{ width: `${pullRequest.merged_unhandled_issues / denominator * 100}%` }} />
      <span className="pending" style={{ width: `${pullRequest.pending_issues / denominator * 100}%` }} />
    </div>
  )
}

function ReviewTrend({ points, from, to }: { points: ReviewDashboardTrendPoint[]; from: string; to: string }) {
  if (!points.length) {
    return <div className="review-trend-empty"><GitCommitHorizontal size={21} /><span>当前时间段还没有问题记录</span></div>
  }
  const start = new Date(`${from}T00:00:00`).getTime()
  const end = new Date(`${to}T23:59:59.999`).getTime()
  const duration = Math.max(1, end - start)
  const maxTotal = Math.max(...points.map((point) => point.issue_total), 1)
  const chartLeft = 48
  const chartRight = 940
  const chartBottom = 168
  const chartHeight = 132
  const barWidth = Math.max(4, Math.min(18, 680 / Math.max(points.length, 1)))
  const startLabel = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(new Date(start))
  const endLabel = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(new Date(end))

  return (
    <div className="review-trend-chart">
      <svg viewBox="0 0 988 200" role="img" aria-label={`${from} 至 ${to} 的检视问题趋势`} preserveAspectRatio="none">
        <line x1={chartLeft} y1={chartBottom} x2={chartRight} y2={chartBottom} className="axis" />
        <line x1={chartLeft} y1={chartBottom - chartHeight} x2={chartRight} y2={chartBottom - chartHeight} className="guide" />
        <text x="4" y={chartBottom - chartHeight + 4}>{maxTotal}</text>
        <text x="4" y={chartBottom + 4}>0</text>
        {points.map((point) => {
          const time = new Date(`${point.date}T12:00:00`).getTime()
          const x = chartLeft + ((time - start) / duration) * (chartRight - chartLeft)
          const unitHeight = chartHeight / maxTotal
          const acceptedHeight = point.accepted_issues * unitHeight
          const unhandledHeight = point.merged_unhandled_issues * unitHeight
          const pendingHeight = point.pending_issues * unitHeight
          return (
            <g key={point.date}>
              <title>{`${point.date}：发现 ${point.issue_total}，接受 ${point.accepted_issues}，合入未处理 ${point.merged_unhandled_issues}，待确认 ${point.pending_issues}`}</title>
              <rect x={x - barWidth / 2} y={chartBottom - acceptedHeight} width={barWidth} height={acceptedHeight} className="accepted" rx="2" />
              <rect x={x - barWidth / 2} y={chartBottom - acceptedHeight - unhandledHeight} width={barWidth} height={unhandledHeight} className="unhandled" rx="2" />
              <rect x={x - barWidth / 2} y={chartBottom - acceptedHeight - unhandledHeight - pendingHeight} width={barWidth} height={pendingHeight} className="pending" rx="2" />
            </g>
          )
        })}
        <text x={chartLeft} y="193">{startLabel}</text>
        <text x={chartRight} y="193" textAnchor="end">{endLabel}</text>
      </svg>
    </div>
  )
}

interface PullRequestDrawerProps {
  pullRequest: ReviewDashboardPullRequest
  createdFrom: string
  createdTo: string
  onClose: () => void
  onOpenTask: (taskId: string) => void
}

function PullRequestDrawer({ pullRequest, createdFrom, createdTo, onClose, onOpenTask }: PullRequestDrawerProps) {
  const [detail, setDetail] = useState<ReviewDashboardPullRequestDetail | null>(null)
  const [issues, setIssues] = useState<ReviewIssue[]>([])
  const [issueTotal, setIssueTotal] = useState(0)
  const [issuePage, setIssuePage] = useState(1)
  const [issuePageSize, setIssuePageSize] = useState(10)
  const [view, setView] = useState<'issues' | 'tasks'>('issues')
  const [expandedIssue, setExpandedIssue] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [issuesLoading, setIssuesLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    setLoading(true)
    setError('')
    api.getReviewDashboardPullRequest(pullRequest.provider, pullRequest.project_path, pullRequest.pr_number)
      .then(setDetail)
      .catch((requestError) => setError(messageFrom(requestError)))
      .finally(() => setLoading(false))
  }, [pullRequest.pr_number, pullRequest.project_path, pullRequest.provider])

  const loadIssues = useCallback(async () => {
    setIssuesLoading(true)
    try {
      const response = await api.listReviewIssues({
        offset: (issuePage - 1) * issuePageSize,
        limit: issuePageSize,
        provider: pullRequest.provider,
        projectPath: pullRequest.project_path,
        prNumber: pullRequest.pr_number,
        batchCreatedFrom: createdFrom,
        batchCreatedTo: createdTo,
      })
      setIssues(response.items)
      setIssueTotal(response.total)
      const lastPage = Math.max(1, Math.ceil(response.total / issuePageSize))
      if (issuePage > lastPage) setIssuePage(lastPage)
    } catch (requestError) {
      setError(messageFrom(requestError))
    } finally {
      setIssuesLoading(false)
    }
  }, [createdFrom, createdTo, issuePage, issuePageSize, pullRequest.pr_number, pullRequest.project_path, pullRequest.provider])

  useEffect(() => {
    loadIssues()
  }, [loadIssues])

  const batchesById = useMemo(
    () => new Map(detail?.batches.map((batch) => [batch.id, batch]) || []),
    [detail],
  )

  return (
    <Offcanvas show onHide={onClose} placement="end" className="detail-drawer review-dashboard-drawer" aria-labelledby="review-pr-drawer-title">
      <div className="drawer-head">
        <button className="icon-button" onClick={onClose} aria-label="关闭 PR 详情"><X size={19} /></button>
        <span className="drawer-record-id">PR EVIDENCE</span>
      </div>

      <div className="review-pr-drawer-intro">
        <div className="review-pr-drawer-repository"><span>{pullRequest.provider}</span>{pullRequest.project_path}</div>
        <div className="review-pr-drawer-title-line">
          <h2 id="review-pr-drawer-title"><GitPullRequest size={22} />!{pullRequest.pr_number}</h2>
          {pullRequest.pr_url && <a href={pullRequest.pr_url} target="_blank" rel="noreferrer">打开 PR<ExternalLink size={14} /></a>}
        </div>
        <p>当前筛选时段发现 {pullRequest.issue_total} 个问题；任务视图包含这个 PR 的全部历史检查任务。</p>
      </div>

      <div className="review-pr-drawer-summary">
        <div><span>发现</span><strong>{pullRequest.issue_total}</strong></div>
        <div className="accepted"><span>已接受</span><strong>{pullRequest.accepted_issues}</strong></div>
        <div className="unhandled"><span>合入未处理</span><strong>{pullRequest.merged_unhandled_issues}</strong></div>
        <div className="pending"><span>待确认</span><strong>{pullRequest.pending_issues}</strong></div>
      </div>

      <div className="review-pr-drawer-tabs">
        <ButtonGroup aria-label="PR 详情视图">
          <Button variant={view === 'issues' ? 'primary' : 'outline-secondary'} onClick={() => setView('issues')}><FileCode2 size={15} />问题详情 <span>{issueTotal}</span></Button>
          <Button variant={view === 'tasks' ? 'primary' : 'outline-secondary'} onClick={() => setView('tasks')}><ListChecks size={15} />检查任务 <span>{detail?.tasks.length ?? pullRequest.task_total}</span></Button>
        </ButtonGroup>
      </div>

      <div className="review-pr-drawer-scroll">
        {error && <div className="inline-error"><CircleAlert size={16} />{error}</div>}
        {view === 'issues' ? (
          <section className="review-dashboard-issues">
            <div className="review-drawer-section-head"><div><strong>时段内问题</strong><span>{createdFrom.slice(0, 10)} — {createdTo.slice(0, 10)}</span></div><button className="icon-button" onClick={loadIssues} aria-label="刷新问题"><RefreshCw size={15} className={issuesLoading ? 'spin' : ''} /></button></div>
            {issuesLoading ? (
              <div className="review-dashboard-drawer-state"><RefreshCw size={20} className="spin" />正在读取问题…</div>
            ) : issues.length === 0 ? (
              <div className="review-dashboard-drawer-state"><ShieldCheck size={21} />这个时段没有问题</div>
            ) : (
              <div className="review-dashboard-issue-list">
                {issues.map((issue) => {
                  const status = ISSUE_STATUS_META[issue.verification_status]
                  const batch = batchesById.get(issue.batch_id)
                  const expanded = expandedIssue === issue.id
                  return (
                    <article className={`review-dashboard-issue issue-${status.tone} ${expanded ? 'expanded' : ''}`} key={issue.id}>
                      <button className="review-dashboard-issue-toggle" onClick={() => setExpandedIssue(expanded ? null : issue.id)} aria-expanded={expanded}>
                        <span className={`review-dashboard-severity severity-${issue.severity}`}>{SEVERITY_LABEL[issue.severity]}</span>
                        <span className="review-dashboard-issue-title"><strong>{issue.title}</strong><small>{issue.file_path ? `${issue.file_path}${issue.line_number ? `:${issue.line_number}` : ''}` : `问题 #${issue.issue_no}`}</small></span>
                        <span className={`review-dashboard-verdict verdict-${status.tone}`}><i />{status.label}</span>
                        <ChevronDown size={16} />
                      </button>
                      {expanded && (
                        <div className="review-dashboard-issue-detail">
                          <p>{issue.description}</p>
                          {issue.verification_note && <blockquote><span>验证依据</span>{issue.verification_note}</blockquote>}
                          <dl>
                            <div><dt>分类</dt><dd>{issue.category || '未分类'}</dd></div>
                            <div><dt>检查批次</dt><dd>{batch ? `BATCH-${batch.id.slice(0, 8).toUpperCase()}` : issue.batch_id}</dd></div>
                            <div><dt>发现时间</dt><dd>{formatFullDate(issue.created_at)}</dd></div>
                            <div><dt>批次阶段</dt><dd>{batch ? BATCH_STATUS_LABEL[batch.status] : '—'}</dd></div>
                          </dl>
                        </div>
                      )}
                    </article>
                  )
                })}
              </div>
            )}
            {!issuesLoading && issues.length > 0 && <Pagination page={issuePage} pageSize={issuePageSize} total={issueTotal} itemLabel="问题" onPageChange={setIssuePage} onPageSizeChange={(value) => { setIssuePageSize(value); setIssuePage(1) }} />}
          </section>
        ) : (
          <section className="review-dashboard-tasks">
            <div className="review-drawer-section-head"><div><strong>全部检查任务</strong><span>覆盖 {detail?.batches.length || 0} 个检查批次</span></div></div>
            {loading ? (
              <div className="review-dashboard-drawer-state"><RefreshCw size={20} className="spin" />正在关联任务…</div>
            ) : !detail?.tasks.length ? (
              <div className="review-dashboard-drawer-state"><ListChecks size={21} />没有关联任务</div>
            ) : (
              <div className="review-dashboard-task-list">
                {detail.tasks.map((task) => (
                  <button key={`${task.batch_id}-${task.role}-${task.id}`} onClick={() => onOpenTask(task.id)}>
                    <span className={`review-dashboard-task-role role-${task.role}`}>{ROLE_LABEL[task.role]}</span>
                    <span className="review-dashboard-task-main"><strong>{compactTask(task.id)}</strong><small>{formatFullDate(task.created_at)} · BATCH-{task.batch_id.slice(0, 6).toUpperCase()}</small>{task.error_message && <em>{task.error_message}</em>}</span>
                    <span className={`review-dashboard-task-status task-${task.status}`}><i />{TASK_STATUS_META[task.status].label}</span>
                    <ChevronRight size={16} />
                  </button>
                ))}
              </div>
            )}
          </section>
        )}
      </div>
    </Offcanvas>
  )
}

interface ReviewDashboardPageProps {
  onOpenSettings: () => void
  onOpenTask: (taskId: string) => void
}

export default function ReviewDashboardPage({ onOpenSettings, onOpenTask }: ReviewDashboardPageProps) {
  const [dashboard, setDashboard] = useState(EMPTY_DASHBOARD)
  const [from, setFrom] = useState(() => daysAgo(29))
  const [to, setTo] = useState(() => daysAgo(0))
  const [preset, setPreset] = useState<'7' | '30' | '90' | 'custom'>('30')
  const [repository, setRepository] = useState('')
  const [tag, setTag] = useState('')
  const [outcome, setOutcome] = useState<ReviewDashboardOutcome>('all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [selected, setSelected] = useState<ReviewDashboardPullRequest | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  const bounds = useMemo(() => apiDateBounds(from, to), [from, to])

  const loadDashboard = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true)
    else setLoading(true)
    const selectedRepository = parseRepositoryKey(repository)
    try {
      const response = await api.getReviewDashboard({
        offset: (page - 1) * pageSize,
        limit: pageSize,
        ...selectedRepository,
        tag: tag || undefined,
        ...bounds,
        outcome,
      })
      setDashboard(response)
      setError('')
      const lastPage = Math.max(1, Math.ceil(response.total / pageSize))
      if (page > lastPage) setPage(lastPage)
    } catch (requestError) {
      setError(messageFrom(requestError))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [bounds, outcome, page, pageSize, repository, tag])

  useEffect(() => {
    loadDashboard()
    const timer = window.setInterval(() => {
      if (!document.hidden) loadDashboard(true)
    }, 20000)
    return () => window.clearInterval(timer)
  }, [loadDashboard])

  function applyPreset(days: 7 | 30 | 90) {
    setPreset(String(days) as '7' | '30' | '90')
    setFrom(daysAgo(days - 1))
    setTo(daysAgo(0))
    setPage(1)
  }

  function handleRowKey(event: KeyboardEvent<HTMLTableRowElement>, pullRequest: ReviewDashboardPullRequest) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      setSelected(pullRequest)
    }
  }

  function chooseOutcome(value: ReviewDashboardOutcome) {
    setOutcome((current) => current === value && value !== 'all' ? 'all' : value)
    setPage(1)
  }

  const summary = dashboard.summary
  const acceptance = summary.acceptance_rate === null ? '—' : `${Math.round(summary.acceptance_rate * 100)}%`

  return (
    <>
      <section className="page-heading review-dashboard-heading">
        <div>
          <p className="eyebrow">REVIEW IMPACT / 质量回路</p>
          <h1>检视影响看板</h1>
          <p>从发现问题到合入结论，追踪每个 PR 的真实处理结果。</p>
        </div>
        <div className="review-dashboard-updated"><RefreshCw size={14} className={refreshing ? 'spin' : ''} /><span>20 秒同步</span></div>
      </section>

      <section className="review-dashboard-filterbar" aria-label="看板筛选">
        <div className="review-dashboard-presets">
          <span>时间范围</span>
          <ButtonGroup>
            {([7, 30, 90] as const).map((days) => <Button key={days} variant={preset === String(days) ? 'primary' : 'outline-secondary'} onClick={() => applyPreset(days)}>近 {days} 天</Button>)}
          </ButtonGroup>
        </div>
        <label className="review-dashboard-date"><span>从</span><Form.Control type="date" value={from} max={to} onChange={(event) => { setFrom(event.target.value); setPreset('custom'); setPage(1) }} /></label>
        <label className="review-dashboard-date"><span>到</span><Form.Control type="date" value={to} min={from} max={daysAgo(0)} onChange={(event) => { setTo(event.target.value); setPreset('custom'); setPage(1) }} /></label>
        <label className="review-dashboard-repository"><span>仓库</span><Form.Select value={repository} onChange={(event) => { setRepository(event.target.value); setPage(1) }}><option value="">全部仓库</option>{dashboard.repositories.map((item) => <option key={repositoryKey(item.provider, item.project_path)} value={repositoryKey(item.provider, item.project_path)}>{item.project_path} · {item.issue_total} 个问题</option>)}</Form.Select></label>
        <label className="review-dashboard-tag"><span>Tag</span><Form.Select value={tag} onChange={(event) => { setTag(event.target.value); setPage(1) }}><option value="">全部 Tags</option>{(dashboard.tags || []).map((item) => <option key={item} value={item}>{item}</option>)}</Form.Select></label>
        <button className="icon-button" onClick={() => loadDashboard(true)} aria-label="刷新聚合看板"><RefreshCw size={17} className={refreshing ? 'spin' : ''} /></button>
      </section>

      {error ? (
        <section className="state-message error-state review-dashboard-error"><CircleAlert size={26} /><strong>无法读取检视看板</strong><p>{error}</p><div>{error === 'invalid api token' && <Button variant="outline-secondary" onClick={onOpenSettings}><KeyRound size={16} />填写 Token</Button>}<Button variant="primary" onClick={() => loadDashboard()}><RefreshCw size={16} />重试连接</Button></div></section>
      ) : (
        <>
          <section className="review-evidence-board" aria-label="问题处理证据轨道">
            <div className="review-evidence-thesis">
              <p>在所选时段内</p>
              <h2><strong>{summary.issue_total.toLocaleString('zh-CN')}</strong> 个问题进入检视决策</h2>
              <span>来自 {summary.pull_request_total} 个 PR、{summary.batch_total} 次检查</span>
            </div>
            <div className="review-evidence-route" aria-hidden="true"><span /><i /><i /><i /></div>
            <div className="review-evidence-outcomes">
              <button className={`accepted ${outcome === 'accepted' ? 'active' : ''}`} onClick={() => chooseOutcome('accepted')}>
                <span><Check size={17} />已接受</span><strong>{summary.accepted_issues}</strong><small>验证确认已处理</small>
              </button>
              <button className={`unhandled ${outcome === 'unhandled' ? 'active' : ''}`} onClick={() => chooseOutcome('unhandled')}>
                <span><AlertTriangle size={17} />合入未处理</span><strong>{summary.merged_unhandled_issues}</strong><small>合入后仍未修复</small>
              </button>
              <button className={`pending ${outcome === 'pending' ? 'active' : ''}`} onClick={() => chooseOutcome('pending')}>
                <span><Clock3 size={17} />待确认</span><strong>{summary.pending_issues}</strong><small>尚无验证结论</small>
              </button>
            </div>
            <button className={`review-evidence-rate ${outcome === 'all' ? 'active' : ''}`} onClick={() => chooseOutcome('all')}>
              <span>问题接受率</span><strong>{acceptance}</strong><small>仅计算已有结论的问题</small>
            </button>
          </section>

          <section className="review-trend-panel">
            <div className="review-trend-head"><div><p className="eyebrow">EVIDENCE PULSE</p><h2>问题处理脉冲</h2></div><div className="review-trend-legend"><span className="accepted"><i />已接受</span><span className="unhandled"><i />合入未处理</span><span className="pending"><i />待确认</span></div></div>
            {loading ? <div className="review-trend-empty"><RefreshCw size={21} className="spin" /><span>正在聚合趋势…</span></div> : <ReviewTrend points={dashboard.timeline} from={from} to={to} />}
          </section>

          <section className="review-pr-panel">
            <div className="review-pr-panel-head">
              <div><p className="eyebrow">PULL REQUEST EVIDENCE</p><h2>PR 处理清单</h2><span>按合入未处理、待确认和最近活动排序</span></div>
              <div className="review-pr-result-count"><strong>{dashboard.total.toLocaleString('zh-CN')}</strong><span>个匹配 PR</span>{outcome !== 'all' && <Button variant="link" size="sm" onClick={() => chooseOutcome('all')}>清除结论筛选</Button>}</div>
            </div>

            <div className="review-pr-table-wrap">
              {loading ? (
                <div className="state-message"><RefreshCw size={24} className="spin" /><strong>正在汇总 PR</strong><p>关联问题结论和检查任务…</p></div>
              ) : dashboard.items.length === 0 ? (
                <div className="state-message"><ShieldCheck size={26} /><strong>没有匹配的 PR</strong><p>调整时间、仓库、Tag 或问题结论后再试。</p></div>
              ) : (
                <>
                  <Table hover className="review-pr-table">
                    <thead><tr><th>仓库 / PR</th><th>问题证据</th><th>处理结论</th><th>全部检查任务</th><th>最近检查</th><th><span className="sr-only">操作</span></th></tr></thead>
                    <tbody>{dashboard.items.map((pullRequest) => (
                      <tr key={`${pullRequest.provider}/${pullRequest.project_path}/${pullRequest.pr_number}`} tabIndex={0} onClick={() => setSelected(pullRequest)} onKeyDown={(event) => handleRowKey(event, pullRequest)}>
                        <td><div className="review-pr-identity"><strong>{pullRequest.project_path}</strong><span><GitPullRequest size={13} />!{pullRequest.pr_number}<i />{pullRequest.batch_total} 次检查</span></div></td>
                        <td><div className="review-pr-evidence-cell"><strong>{pullRequest.issue_total}</strong><OutcomeMiniBar pullRequest={pullRequest} /></div></td>
                        <td><div className="review-pr-outcome-counts"><span className="accepted">接受 <b>{pullRequest.accepted_issues}</b></span><span className="unhandled">未处理 <b>{pullRequest.merged_unhandled_issues}</b></span><span className="pending">待确认 <b>{pullRequest.pending_issues}</b></span></div></td>
                        <td><div className="review-pr-tasks-cell"><TaskStatusCluster counts={pullRequest.task_status_counts} /><small>共 {pullRequest.task_total} 个任务</small></div></td>
                        <td><div className="review-pr-latest"><span>{formatDate(pullRequest.latest_activity_at)}</span><small>{BATCH_STATUS_LABEL[pullRequest.latest_batch_status]}</small></div></td>
                        <td><button className="row-action" onClick={(event) => { event.stopPropagation(); setSelected(pullRequest) }} aria-label={`查看 ${pullRequest.project_path} PR ${pullRequest.pr_number}`}><ChevronRight size={17} /></button></td>
                      </tr>
                    ))}</tbody>
                  </Table>
                  <div className="review-pr-mobile-list">{dashboard.items.map((pullRequest) => (
                    <button className="review-pr-mobile-card" key={`${pullRequest.provider}/${pullRequest.project_path}/${pullRequest.pr_number}`} onClick={() => setSelected(pullRequest)}>
                      <span className="review-pr-mobile-top"><span><strong>{pullRequest.project_path}</strong><small><GitPullRequest size={12} />!{pullRequest.pr_number} · {pullRequest.batch_total} 次检查</small></span><ChevronRight size={18} /></span>
                      <OutcomeMiniBar pullRequest={pullRequest} />
                      <span className="review-pr-mobile-outcomes"><i className="accepted">接受 {pullRequest.accepted_issues}</i><i className="unhandled">未处理 {pullRequest.merged_unhandled_issues}</i><i className="pending">待确认 {pullRequest.pending_issues}</i></span>
                      <span className="review-pr-mobile-footer"><TaskStatusCluster counts={pullRequest.task_status_counts} /><small>{formatDate(pullRequest.latest_activity_at)}</small></span>
                    </button>
                  ))}</div>
                </>
              )}
            </div>

            {!loading && dashboard.items.length > 0 && <div className="panel-footer"><Pagination page={page} pageSize={pageSize} total={dashboard.total} itemLabel="PR" onPageChange={setPage} onPageSizeChange={(value) => { setPageSize(value); setPage(1) }} /><span className="panel-source"><Server size={13} />聚合检视、PR 与任务数据</span></div>}
          </section>
        </>
      )}

      {selected && <PullRequestDrawer pullRequest={selected} createdFrom={bounds.createdFrom} createdTo={bounds.createdTo} onClose={() => setSelected(null)} onOpenTask={onOpenTask} />}
    </>
  )
}
