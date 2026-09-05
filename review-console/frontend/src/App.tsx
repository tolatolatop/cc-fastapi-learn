import { FormEvent, useCallback, useEffect, useState } from 'react'
import { ArrowRight, ArrowUpDown, BarChart3, Check, Clock3, Code2, Copy, ExternalLink, FileText, HelpCircle, History, Link2, LogOut, Search, ShieldCheck, Users, X } from 'lucide-react'
import { Button, Form, Modal, Table } from 'react-bootstrap'
import { api } from './api'
import type { DecisionReason, HistoryItem, Issue, IssueStatus, Permission, PullRequest, PullRequestCompletionStatus, Repository, Statistics, User } from './types'

const statusMeta: Record<IssueStatus, { label: string; className: string }> = {
  unverified: { label: '待裁定', className: 'pending' },
  accepted: { label: '接受', className: 'accepted' },
  not_accepted: { label: '拒绝', className: 'rejected' },
  needs_info: { label: '待补充', className: 'needs-info' },
}

const reasonMeta: Record<DecisionReason, string> = {
  false_positive: '误报',
  protected_by_control: '已有控制措施',
  not_reproducible: '无法复现',
  duplicate: '重复意见',
  out_of_scope: '超出范围',
  intentional_behavior: '符合设计预期',
  risk_accepted: '风险已接受',
  other: '其他',
}

const completionMeta: Record<PullRequestCompletionStatus, string> = {
  processing: '处理中',
  pending: '待裁定',
  completed: '已检视完毕',
  no_issues: '零问题',
  failed: '流程失败',
}

type Page = 'issues' | 'history' | 'statistics' | 'users'

function pageFromUrl(): Page {
  const value = new URLSearchParams(window.location.search).get('view')
  return value === 'history' || value === 'statistics' || value === 'users' ? value : 'issues'
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}

function pullRequestKey(pullRequest: Pick<PullRequest, 'provider' | 'project_path' | 'pr_number'>) {
  return `${pullRequest.provider}/${pullRequest.project_path}/${pullRequest.pr_number}`
}

function repositoryFor(pullRequest: PullRequest | null, repositories: Repository[]) {
  if (!pullRequest) return null
  return repositories.find((repository) => repository.provider === pullRequest.provider && repository.project_path === pullRequest.project_path) || null
}

function issueLocation(issue: Issue) {
  if (!issue.file_path) return '未标注文件'
  return issue.line_number === null ? `${issue.file_path}（未标注行号）` : `${issue.file_path}:${issue.line_number}`
}

function compareIssueLocation(left: Issue, right: Issue) {
  const leftPath = left.file_path?.toLocaleLowerCase() || '\uffff'
  const rightPath = right.file_path?.toLocaleLowerCase() || '\uffff'
  const pathOrder = leftPath.localeCompare(rightPath, undefined, { numeric: true, sensitivity: 'base' })
  if (pathOrder) return pathOrder
  const lineOrder = (left.line_number ?? Number.MAX_SAFE_INTEGER) - (right.line_number ?? Number.MAX_SAFE_INTEGER)
  return lineOrder || left.issue_no - right.issue_no
}

function requestedPullRequest(pullRequests: PullRequest[]) {
  const params = new URLSearchParams(window.location.search)
  const provider = params.get('provider')
  const projectPath = params.get('project_path')
  const prNumber = params.get('pr') || params.get('pr_number')
  if (!provider || !projectPath || !prNumber) return { requested: false, pullRequest: null }
  return {
    requested: true,
    pullRequest: pullRequests.find((item) => item.provider === provider && item.project_path === projectPath && item.pr_number === prNumber) || null,
  }
}

function writePullRequestUrl(pullRequest: PullRequest, replace = false) {
  const params = new URLSearchParams(window.location.search)
  params.set('view', 'issues')
  params.set('provider', pullRequest.provider)
  params.set('project_path', pullRequest.project_path)
  params.set('pr', pullRequest.pr_number)
  const url = `${window.location.pathname}?${params.toString()}${window.location.hash}`
  window.history[replace ? 'replaceState' : 'pushState']({}, '', url)
}

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true); setError('')
    try {
      const result = await api.login(username, password)
      onLogin(result.user)
    } catch (reason) { setError((reason as Error).message) } finally { setBusy(false) }
  }
  return <main className="login-shell">
    <section className="login-brand">
      <div className="brand-mark"><Code2 size={24} /></div>
      <p className="eyebrow">CODE REVIEW / HUMAN DECISION</p>
      <h1>让每一条检视意见<br />都有明确裁定。</h1>
      <p>独立权限、人工判断、完整轨迹。自动检视负责发现问题，审核者负责作出有据可查的决定。</p>
      <div className="trace-sample"><span>发现</span><ArrowRight /><span>复核</span><ArrowRight /><strong>裁定</strong></div>
    </section>
    <section className="login-panel">
      <form onSubmit={submit}>
        <p className="eyebrow">REVIEW CONSOLE</p><h2>登录检视裁定台</h2><p className="muted">使用管理员分配的审核账号</p>
        <Form.Group className="mb-3"><Form.Label>用户名</Form.Label><Form.Control autoFocus value={username} onChange={(e) => setUsername(e.target.value)} /></Form.Group>
        <Form.Group className="mb-4"><Form.Label>密码</Form.Label><Form.Control type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></Form.Group>
        {error && <div className="form-error">{error}</div>}
        <Button type="submit" className="w-100" disabled={busy || !username || !password}>{busy ? '正在验证…' : '登录'}</Button>
      </form>
    </section>
  </main>
}

function RejectModal({ issue, onClose, onSaved }: { issue: Issue | null; onClose: () => void; onSaved: (issue: Issue) => void }) {
  const [reasonCode, setReasonCode] = useState<DecisionReason | ''>('')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (issue) { setReasonCode(issue.status === 'not_accepted' ? issue.reason_code || '' : ''); setNote(issue.status === 'not_accepted' ? issue.note || '' : ''); setError('') } }, [issue])
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!issue) return
    if (!reasonCode || !note.trim()) { setError('拒绝时必须选择原因分类并填写详细理由'); return }
    setBusy(true); setError('')
    try { onSaved(await api.updateStatus(issue, 'not_accepted', reasonCode, note.trim())); onClose() }
    catch (reason) { setError((reason as Error).message) } finally { setBusy(false) }
  }
  return <Modal show={Boolean(issue)} onHide={onClose} centered>
    <form onSubmit={submit}><Modal.Header closeButton><Modal.Title>拒绝检视意见</Modal.Title></Modal.Header><Modal.Body>
      <p className="decision-title">{issue?.title}</p>
      <Form.Group className="mb-3"><Form.Label>拒绝原因分类（必填）</Form.Label><Form.Select autoFocus value={reasonCode} onChange={(event) => setReasonCode(event.target.value as DecisionReason | '')} required><option value="">选择原因</option>{Object.entries(reasonMeta).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</Form.Select></Form.Group>
      <Form.Group><Form.Label>拒绝理由详情（必填）</Form.Label><Form.Control as="textarea" rows={4} value={note} onChange={(event) => setNote(event.target.value)} placeholder="说明不接受这条意见的事实依据，便于后续追溯…" required /></Form.Group>
      {error && <div className="form-error">{error}</div>}
    </Modal.Body><Modal.Footer><Button variant="outline-secondary" onClick={onClose}>取消</Button><Button variant="danger" type="submit" disabled={busy}>{busy ? '提交中…' : '确认拒绝'}</Button></Modal.Footer></form>
  </Modal>
}

function IssueDetail({ issue, permission, onClose, onSaved }: { issue: Issue | null; permission: Permission | null; onClose: () => void; onSaved: (issue: Issue) => void }) {
  const [reasonCode, setReasonCode] = useState<DecisionReason | ''>('')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (issue) { setReasonCode(issue.reason_code || ''); setNote(issue.note || ''); setError('') } }, [issue])
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!issue || permission !== 'write') return
    if (issue.status === 'not_accepted' && (!reasonCode || !note.trim())) { setError('拒绝意见必须保留原因分类和详细理由'); return }
    setBusy(true); setError('')
    try { onSaved(await api.updateStatus(issue, issue.status, issue.status === 'not_accepted' ? reasonCode || null : null, note.trim() || null)) }
    catch (reason) { setError((reason as Error).message) } finally { setBusy(false) }
  }
  const noteLabel = issue?.status === 'not_accepted' ? '拒绝理由' : issue?.status === 'needs_info' ? '待补充内容' : '裁定说明'
  return <Modal dialogClassName="issue-detail-modal" show={Boolean(issue)} onHide={onClose} centered scrollable size="lg">
    {issue && <form onSubmit={submit}><Modal.Header closeButton><Modal.Title>意见详情</Modal.Title></Modal.Header><Modal.Body>
      <div className="detail-heading"><span className={`severity ${issue.severity}`}>{issue.severity}</span><span className={`status ${statusMeta[issue.status].className}`}>{statusMeta[issue.status].label}</span><h2>{issue.title}</h2><p>{issue.description}</p></div>
      <dl className="detail-facts"><div><dt>位置</dt><dd>{issueLocation(issue)}</dd></div><div><dt>分类</dt><dd>{issue.category || '未分类'}</dd></div><div><dt>意见编号</dt><dd>#{issue.issue_no}</dd></div><div><dt>自动修复验证</dt><dd>{issue.verification_status === 'accepted' ? '通过' : issue.verification_status === 'not_accepted' ? '未通过' : '未验证'}</dd></div><div><dt>检视版本</dt><dd>{issue.review_head_sha?.slice(0, 12) || '未知'}</dd></div><div><dt>合并版本</dt><dd>{issue.merged_sha?.slice(0, 12) || '未合并'}</dd></div><div><dt>检视批次</dt><dd>{issue.batch_status} · {formatTime(issue.batch_created_at)}</dd></div><div><dt>裁定人</dt><dd>{issue.decided_by_name ? `${issue.decided_by_name} · ${issue.decided_at ? formatTime(issue.decided_at) : ''}` : '尚未裁定'}</dd></div></dl>
      {issue.status === 'not_accepted' && <Form.Group className="mb-3"><Form.Label>拒绝原因分类</Form.Label><Form.Select value={reasonCode} disabled={permission !== 'write'} onChange={(event) => setReasonCode(event.target.value as DecisionReason | '')} required><option value="">选择原因</option>{Object.entries(reasonMeta).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</Form.Select></Form.Group>}
      <Form.Group><Form.Label>{noteLabel}</Form.Label><Form.Control as="textarea" rows={5} value={note} disabled={permission !== 'write'} onChange={(event) => setNote(event.target.value)} placeholder={issue.status === 'needs_info' ? '写明需要补充的事实或上下文…' : '补充判断依据…'} /></Form.Group>
      {error && <div className="form-error">{error}</div>}
      {permission !== 'write' && <p className="read-only-hint">你对该仓库拥有只读权限。</p>}
    </Modal.Body><Modal.Footer><Button variant="outline-secondary" type="button" onClick={onClose}>关闭</Button>{permission === 'write' && <Button type="submit" disabled={busy}>{busy ? '保存中…' : `保存${noteLabel}`}</Button>}</Modal.Footer></form>}
  </Modal>
}

function IssueWorkspace({ repositories, routeVersion }: { repositories: Repository[]; routeVersion: number }) {
  const [pullRequests, setPullRequests] = useState<PullRequest[]>([])
  const [selectedPr, setSelectedPr] = useState<PullRequest | null>(null)
  const [issues, setIssues] = useState<Issue[]>([])
  const [status, setStatus] = useState('')
  const [query, setQuery] = useState('')
  const [queueQuery, setQueueQuery] = useState('')
  const [pendingFirst, setPendingFirst] = useState(false)
  const [pullRequestsLoading, setPullRequestsLoading] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [rejecting, setRejecting] = useState<Issue | null>(null)
  const [detail, setDetail] = useState<Issue | null>(null)
  const [busyDecision, setBusyDecision] = useState<{ issueId: string; status: IssueStatus } | null>(null)
  const [copied, setCopied] = useState(false)
  const [copiedLocationId, setCopiedLocationId] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setPullRequestsLoading(true); setError('')
    Promise.all(repositories.map((repository) => api.pullRequests(repository)))
      .then((results) => {
        if (cancelled) return
        const items = results.flatMap((result) => result.items).sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime())
        const target = requestedPullRequest(items)
        setPullRequests(items)
        if (target.requested && !target.pullRequest) {
          setSelectedPr(null)
          setError('链接中的 PR/MR 不存在，或你没有访问权限。')
        } else {
          const next = target.pullRequest || items[0] || null
          setSelectedPr(next)
          if (next && !target.requested) writePullRequestUrl(next, true)
        }
      })
      .catch((reason) => { if (!cancelled) setError((reason as Error).message) })
      .finally(() => { if (!cancelled) setPullRequestsLoading(false) })
    return () => { cancelled = true }
  }, [repositories, routeVersion])
  const selectedRepository = repositoryFor(selectedPr, repositories)
  const load = useCallback(async () => {
    if (!selectedRepository || !selectedPr) { setIssues([]); return }
    setLoading(true); setError('')
    try { setIssues((await api.issues(selectedRepository, selectedPr.pr_number, status, query)).items) } catch (reason) { setError((reason as Error).message) } finally { setLoading(false) }
  }, [selectedRepository, selectedPr, status, query])
  useEffect(() => { void load() }, [load])
  async function refreshPullRequest(updated: Issue) {
    const repository = repositories.find((item) => item.provider === updated.provider && item.project_path === updated.project_path)
    if (!repository) return
    const pullRequest = await api.pullRequest(repository, updated.pr_number)
    setPullRequests((items) => items.map((item) => pullRequestKey(item) === pullRequestKey(pullRequest) ? pullRequest : item))
    setSelectedPr((current) => current && pullRequestKey(current) === pullRequestKey(pullRequest) ? pullRequest : current)
  }
  async function handleSaved(updated: Issue) {
    setIssues((items) => status && updated.status !== status ? items.filter((item) => item.id !== updated.id) : items.map((item) => item.id === updated.id ? updated : item))
    setDetail((current) => current?.id === updated.id ? updated : current)
    try { await refreshPullRequest(updated) }
    catch { setError('裁定已保存，但 PR/MR 汇总刷新失败；重新打开页面即可恢复。') }
  }
  async function setIssueStatus(issue: Issue, nextStatus: IssueStatus) {
    if (nextStatus === issue.status) return
    if (nextStatus === 'not_accepted') { setRejecting(issue); return }
    setBusyDecision({ issueId: issue.id, status: nextStatus }); setError('')
    try { await handleSaved(await api.updateStatus(issue, nextStatus, null, null)) }
    catch (reason) { setError((reason as Error).message) }
    finally { setBusyDecision(null) }
  }
  function selectPullRequest(pullRequest: PullRequest) {
    setSelectedPr(pullRequest); setDetail(null); setRejecting(null); setCopied(false); setError(''); writePullRequestUrl(pullRequest)
  }
  async function copyDirectLink() {
    if (!selectedPr) return
    writePullRequestUrl(selectedPr, true)
    try { await navigator.clipboard.writeText(window.location.href); setCopied(true); window.setTimeout(() => setCopied(false), 1600) }
    catch { setError('无法复制链接，请直接复制浏览器地址。') }
  }
  async function copyIssueLocation(issue: Issue) {
    if (!issue.file_path || issue.line_number === null) return
    try { await navigator.clipboard.writeText(`${issue.file_path}:${issue.line_number}`); setCopiedLocationId(issue.id); window.setTimeout(() => setCopiedLocationId(null), 1600) }
    catch { setError('无法复制文件位置，请手动选择路径和行号。') }
  }
  const queueKeywords = queueQuery.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  const visiblePullRequests = pullRequests
    .filter((pullRequest) => {
      if (!queueKeywords.length) return true
      const searchable = `${pullRequest.provider} ${pullRequest.project_path} ${pullRequest.pr_number} #${pullRequest.pr_number} ${pullRequest.pr_url || ''}`.toLocaleLowerCase()
      return queueKeywords.every((keyword) => searchable.includes(keyword))
    })
    .sort((left, right) => {
      if (queueKeywords.length) {
        const pendingOrder = Number(right.pending_total > 0) - Number(left.pending_total > 0)
        if (pendingOrder) return pendingOrder
      }
      return new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime()
    })
  const visibleIssues = [...issues].sort((left, right) => {
    if (pendingFirst) {
      const pendingOrder = Number(right.status === 'unverified') - Number(left.status === 'unverified')
      if (pendingOrder) return pendingOrder
    }
    return compareIssueLocation(left, right)
  })
  return <div className="workspace-grid review-pr-workspace">
    <aside className="pr-rail"><div className="pr-rail-head"><p className="rail-label">PR / MR 审核队列</p><strong>{visiblePullRequests.length}/{pullRequests.length}</strong></div><label className="pr-rail-search"><Search size={15} /><Form.Control value={queueQuery} onChange={(event) => setQueueQuery(event.target.value)} placeholder="仓库、PR ID 或 PR 地址" aria-label="搜索审核队列" /><small>多个关键字用空格分隔</small></label><div className="pr-rail-list">{pullRequestsLoading ? <div className="rail-empty">正在汇总…</div> : visiblePullRequests.map((pullRequest) => <button key={pullRequestKey(pullRequest)} className={selectedPr && pullRequestKey(selectedPr) === pullRequestKey(pullRequest) ? 'active' : ''} onClick={() => selectPullRequest(pullRequest)}><span className="pr-rail-number">#{pullRequest.pr_number}<small>{pullRequest.provider}</small></span><strong>{pullRequest.project_path}</strong><span className="pr-rail-progress"><i style={{ width: `${pullRequest.issue_total ? pullRequest.reviewed_total / pullRequest.issue_total * 100 : 100}%` }} /><small>{pullRequest.pending_total} 条待裁定</small></span><span className={`completion ${pullRequest.completion_status}`}>{completionMeta[pullRequest.completion_status]}</span></button>)}{!pullRequestsLoading && !visiblePullRequests.length && <div className="rail-empty">没有匹配的 PR/MR；请调整关键字</div>}</div></aside>
    <section className="review-content">
      {selectedPr && selectedRepository ? <><header className="content-header pr-content-header"><div><p className="eyebrow">{selectedPr.provider} / {selectedPr.project_path}</p><h1>PR / MR #{selectedPr.pr_number}</h1><span className={`permission ${selectedRepository.permission}`}>{selectedRepository.permission === 'write' ? '可修改' : '只读'}</span><div className="pr-header-links"><Button variant="link" size="sm" onClick={() => void copyDirectLink()}><Link2 size={15} />{copied ? '已复制链接' : '复制直达链接'}</Button>{selectedPr.pr_url && <Button as="a" variant="link" size="sm" href={selectedPr.pr_url} target="_blank" rel="noreferrer"><ExternalLink size={15} />打开代码平台</Button>}</div></div><div className="status-tally"><span><b>{selectedPr.issue_total}</b>全部意见</span><span><b>{selectedPr.reviewed_total}</b>已裁定</span><span><b>{selectedPr.pending_total}</b>待裁定</span><strong className={`completion ${selectedPr.completion_status}`}>{completionMeta[selectedPr.completion_status]}</strong></div></header>
      <div className="filters"><label><Search size={17} /><Form.Control value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索标题、说明或文件" /></label><Form.Select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">全部状态</option><option value="unverified">待裁定</option><option value="accepted">接受</option><option value="not_accepted">拒绝</option><option value="needs_info">待补充</option></Form.Select><Button className={`pending-first-sort ${pendingFirst ? 'active' : ''}`} variant="outline-secondary" aria-pressed={pendingFirst} onClick={() => setPendingFirst((active) => !active)}><ArrowUpDown size={15} />待裁定优先</Button></div>
      {error && <div className="form-error">{error}</div>}{loading ? <div className="empty-state">正在读取检视意见…</div> : !visibleIssues.length ? <div className="empty-state">{selectedPr.completion_status === 'no_issues' ? '该 PR/MR 已检视完毕，没有发现问题' : '当前筛选下没有检视意见'}</div> : <div className="compact-issue-list">{visibleIssues.map((issue) => <article className="compact-issue-card" key={issue.id}>
        <div className={`severity-cell ${issue.severity}`}><span>问题等级</span><strong>{issue.severity}</strong></div>
        <div className="issue-location"><span>文件 / 行号</span><Button variant="link" className="copy-location" disabled={!issue.file_path || issue.line_number === null} title={issue.file_path && issue.line_number !== null ? `复制 ${issue.file_path}:${issue.line_number}` : '缺少路径或行号，无法复制'} onClick={() => void copyIssueLocation(issue)}><strong>{issueLocation(issue)}</strong><small>{copiedLocationId === issue.id ? '已复制' : <Copy size={13} />}</small></Button></div>
        <div className="issue-opinion"><span>检视意见</span><strong>{issue.title}</strong><p>{issue.description}</p></div>
        <div className="compact-issue-actions"><Button className="detail-button" variant="link" size="sm" onClick={() => setDetail(issue)}><FileText size={15} />详情</Button><div className="inline-decisions" aria-label="状态裁定">{(['unverified', 'accepted', 'not_accepted', 'needs_info'] as IssueStatus[]).map((value) => <Button key={value} variant="outline-secondary" size="sm" className={`${statusMeta[value].className} ${issue.status === value ? 'active' : ''}`} disabled={selectedRepository.permission !== 'write' || busyDecision?.issueId === issue.id || issue.status === value} onClick={() => void setIssueStatus(issue, value)}>{value === 'accepted' ? <Check /> : value === 'not_accepted' ? <X /> : value === 'needs_info' ? <HelpCircle /> : <Clock3 />}{busyDecision?.issueId === issue.id && busyDecision.status === value ? '保存中' : statusMeta[value].label}</Button>)}</div></div>
      </article>)}</div>}</> : <div className="empty-state">{pullRequestsLoading ? '正在汇总 PR/MR…' : error || '授权范围内暂无 PR/MR 检视记录'}</div>}
    </section>
    <RejectModal issue={rejecting} onClose={() => setRejecting(null)} onSaved={(updated) => { void handleSaved(updated) }} />
    <IssueDetail issue={detail} permission={selectedRepository?.permission || null} onClose={() => setDetail(null)} onSaved={(updated) => { void handleSaved(updated) }} />
  </div>
}

function HistoryWorkspace({ repositories }: { repositories: Repository[] }) {
  const [repository, setRepository] = useState<Repository | null>(repositories[0] || null)
  const [pullRequests, setPullRequests] = useState<PullRequest[]>([])
  const [pullRequest, setPullRequest] = useState<PullRequest | null>(null)
  const [issues, setIssues] = useState<Issue[]>([])
  const [issue, setIssue] = useState<Issue | null>(null)
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [error, setError] = useState('')

  useEffect(() => { if (repositories.length && !repository) setRepository(repositories[0]) }, [repositories, repository])
  useEffect(() => {
    if (!repository) return
    setPullRequest(null); setIssue(null); setHistory([]); setError('')
    api.pullRequests(repository).then((result) => { setPullRequests(result.items); setPullRequest(result.items[0] || null) }).catch((reason) => setError((reason as Error).message))
  }, [repository])
  useEffect(() => {
    if (!repository || !pullRequest) { setIssues([]); return }
    setIssue(null); setHistory([]); setError('')
    api.issues(repository, pullRequest.pr_number, '', '').then((result) => { setIssues(result.items); setIssue(result.items[0] || null) }).catch((reason) => setError((reason as Error).message))
  }, [repository, pullRequest])
  useEffect(() => {
    if (!issue) { setHistory([]); return }
    setError('')
    api.history(issue.id).then((result) => setHistory(result.items)).catch((reason) => setError((reason as Error).message))
  }, [issue])

  return <div className="workspace-grid history-workspace">
    <aside className="repo-rail"><p className="rail-label">授权仓库</p>{repositories.map((repo) => <button key={`${repo.provider}/${repo.project_path}`} className={repository?.provider === repo.provider && repository.project_path === repo.project_path ? 'active' : ''} onClick={() => setRepository(repo)}><span className="provider">{repo.provider}</span><strong>{repo.project_path}</strong><small>{repo.issue_total} 条检视意见</small></button>)}</aside>
    <section className="review-content"><header className="content-header"><div><p className="eyebrow">AUDIT TRAIL</p><h1>状态变更历史</h1></div></header>
      {error && <div className="form-error">{error}</div>}
      <div className="history-scope"><Form.Select value={pullRequest?.pr_number || ''} onChange={(event) => setPullRequest(pullRequests.find((item) => item.pr_number === event.target.value) || null)}><option value="">选择 PR/MR</option>{pullRequests.map((item) => <option key={item.pr_number} value={item.pr_number}>PR / MR #{item.pr_number} · {completionMeta[item.completion_status]}</option>)}</Form.Select></div>
      <div className="audit-layout"><aside className="audit-issue-list"><p className="rail-label">选择检视意见</p>{issues.map((item) => <button key={item.id} className={issue?.id === item.id ? 'active' : ''} onClick={() => setIssue(item)}><span className={`severity ${item.severity}`}>{item.severity}</span><strong>{item.title}</strong><small>{statusMeta[item.status].label} · #{item.issue_no}</small></button>)}{pullRequest && !issues.length && <div className="audit-empty">该 PR/MR 没有检视意见</div>}</aside>
        <section className="audit-record"><div className="audit-record-head"><p className="eyebrow">CHANGE LOG</p><h2>{issue?.title || '选择一条意见查看历史'}</h2>{issue && <span className={`status ${statusMeta[issue.status].className}`}>当前：{statusMeta[issue.status].label}</span>}</div>
          {!issue ? <div className="empty-state">请从左侧选择检视意见</div> : !history.length ? <div className="empty-state">这条意见还没有状态变更记录</div> : <div className="timeline">{history.map((item) => <article key={item.id}><span className="timeline-dot" /><div className="timeline-meta"><strong>{item.actor_name}<small>{item.dimension === 'decision' ? '人工裁定' : '自动验证'}</small></strong><time>{formatTime(item.created_at)}</time></div><div className="status-transition"><span className={`status ${statusMeta[item.previous_status].className}`}>{statusMeta[item.previous_status].label}</span><ArrowRight size={14} /><span className={`status ${statusMeta[item.new_status].className}`}>{statusMeta[item.new_status].label}</span>{item.new_reason_code && <span className="reason-chip">{reasonMeta[item.new_reason_code]}</span>}</div>{item.new_note && <p>{item.new_note}</p>}</article>)}</div>}
        </section></div>
    </section>
  </div>
}

function StatisticsWorkspace() {
  const [createdFrom, setCreatedFrom] = useState('')
  const [createdTo, setCreatedTo] = useState('')
  const [statistics, setStatistics] = useState<Statistics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const load = useCallback(async (from = createdFrom, to = createdTo) => {
    setLoading(true); setError('')
    try { setStatistics(await api.statistics(from, to)) }
    catch (reason) { setError((reason as Error).message) }
    finally { setLoading(false) }
  }, [createdFrom, createdTo])
  useEffect(() => { void load('', '') }, []) // eslint-disable-line react-hooks/exhaustive-deps
  function submit(event: FormEvent) { event.preventDefault(); void load() }
  const contributorMax = Math.max(...(statistics?.contributors.map((item) => item.confirmed_total) || [1]))
  const falsePositiveMax = Math.max(...(statistics?.top_false_positive_repositories.map((item) => item.false_positive_total) || [1]))
  return <main className="statistics-page">
    <header className="content-header"><div><p className="eyebrow">DECISION INTELLIGENCE</p><h1>裁定数据统计</h1><p className="statistics-intro">数据按每条意见的当前最终裁定统计，时间范围以最终裁定时间为准。</p></div></header>
    <form className="time-filter" onSubmit={submit}><Form.Group><Form.Label>开始日期和时间</Form.Label><Form.Control type="datetime-local" value={createdFrom} max={createdTo || undefined} onChange={(event) => setCreatedFrom(event.target.value)} /></Form.Group><span>至</span><Form.Group><Form.Label>结束日期和时间</Form.Label><Form.Control type="datetime-local" value={createdTo} min={createdFrom || undefined} onChange={(event) => setCreatedTo(event.target.value)} /></Form.Group><Button type="submit" disabled={loading}>应用时间范围</Button><Button variant="outline-secondary" type="button" onClick={() => { setCreatedFrom(''); setCreatedTo(''); void load('', '') }}>重置</Button></form>
    {error && <div className="form-error">{error}</div>}
    {loading && !statistics ? <div className="empty-state">正在汇总裁定数据…</div> : statistics && <>
      <section className="statistics-hero"><div><span>有效意见</span><strong>{statistics.summary.valid_opinion_total}</strong><p>当前被人工确认接受的检视意见</p></div><dl><div><dt>确认贡献</dt><dd>{statistics.summary.confirmed_total}</dd><small>接受与拒绝的最终裁定总数</small></div><div><dt>确认误报</dt><dd>{statistics.summary.false_positive_total}</dd><small>拒绝原因为“误报”的意见</small></div></dl></section>
      <div className="statistics-grid"><section className="ranking-panel"><header><p className="eyebrow">CONTRIBUTION</p><h2>用户确认贡献</h2></header>{statistics.contributors.length ? <div className="ranking-list">{statistics.contributors.map((item, index) => <article key={item.actor_id}><b>{String(index + 1).padStart(2, '0')}</b><div><span><strong>{item.actor_name}</strong><small>接受 {item.accepted_total} · 拒绝 {item.not_accepted_total}</small></span><div className="rank-track"><i style={{ width: `${item.confirmed_total / contributorMax * 100}%` }} /></div></div><em>{item.confirmed_total}</em></article>)}</div> : <div className="panel-empty">所选时段内没有用户完成裁定</div>}</section>
      <section className="ranking-panel false-positive-panel"><header><p className="eyebrow">FALSE POSITIVE / TOP 5</p><h2>误报最多的仓库</h2></header>{statistics.top_false_positive_repositories.length ? <div className="ranking-list">{statistics.top_false_positive_repositories.map((item, index) => <article key={`${item.provider}/${item.project_path}`}><b>{String(index + 1).padStart(2, '0')}</b><div><span><strong>{item.project_path}</strong><small>{item.provider} · 占全部裁定 {(item.false_positive_rate * 100).toFixed(1)}%</small></span><div className="rank-track"><i style={{ width: `${item.false_positive_total / falsePositiveMax * 100}%` }} /></div></div><em>{item.false_positive_total}</em></article>)}</div> : <div className="panel-empty">所选时段内没有确认误报</div>}</section></div>
    </>}
  </main>
}

function UserAdmin({ repositories, currentUserId }: { repositories: Repository[]; currentUserId: string }) {
  const [users, setUsers] = useState<User[]>([])
  const [selected, setSelected] = useState<User | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ username: '', display_name: '', password: '', is_admin: false })
  const [grantRepo, setGrantRepo] = useState('')
  const [permission, setPermission] = useState<Permission>('read')
  const [error, setError] = useState('')
  const load = useCallback(() => api.users().then((r) => { setUsers(r.items); setSelected((current) => r.items.find((u) => u.id === current?.id) || r.items[0] || null) }).catch((e) => setError(e.message)), [])
  useEffect(() => { void load() }, [load])
  async function create(event: FormEvent) { event.preventDefault(); try { await api.createUser(form); setShowCreate(false); setForm({ username: '', display_name: '', password: '', is_admin: false }); await load() } catch (e) { setError((e as Error).message) } }
  async function saveGrant(event: FormEvent) { event.preventDefault(); if (!selected || !grantRepo) return; const repo = repositories[Number(grantRepo)]; try { await api.putGrant(selected.id, { provider: repo.provider, project_path: repo.project_path, permission }); await load() } catch (reason) { setError((reason as Error).message) } }
  return <div className="admin-page"><header className="content-header"><div><p className="eyebrow">ACCESS CONTROL</p><h1>角色与仓库权限</h1></div><Button onClick={() => setShowCreate(true)}>新增用户</Button></header>{error && <div className="form-error">{error}</div>}<div className="admin-grid"><section className="user-list"><Table responsive hover><thead><tr><th>用户</th><th>角色</th><th>状态</th></tr></thead><tbody>{users.map((user) => <tr key={user.id} className={selected?.id === user.id ? 'selected' : ''} onClick={() => setSelected(user)}><td><strong>{user.display_name}</strong><small>@{user.username}</small></td><td>{user.is_admin ? '管理员' : '审核者'}</td><td>{user.is_active ? '启用' : '停用'}</td></tr>)}</tbody></Table></section><section className="grant-panel">{selected && <><div className="grant-heading"><div><h2>{selected.display_name}</h2><p>{selected.is_admin ? '管理员默认拥有全部仓库修改权限' : '按仓库授予只读或修改权限'}</p></div><div className="user-controls"><Form.Check type="switch" label="管理员" checked={selected.is_admin} disabled={selected.id === currentUserId} onChange={async (event) => { try { await api.updateUser(selected.id, { is_admin: event.target.checked }); await load() } catch (reason) { setError((reason as Error).message) } }} /><Form.Check type="switch" label={selected.is_active ? '已启用' : '已停用'} checked={selected.is_active} disabled={selected.id === currentUserId} onChange={async (event) => { try { await api.updateUser(selected.id, { is_active: event.target.checked }); await load() } catch (reason) { setError((reason as Error).message) } }} /></div></div>{!selected.is_admin && <><form className="grant-form" onSubmit={saveGrant}><Form.Select value={grantRepo} onChange={(e) => setGrantRepo(e.target.value)} required><option value="">选择仓库</option>{repositories.map((repo, index) => <option key={`${repo.provider}/${repo.project_path}`} value={index}>{repo.provider} / {repo.project_path}</option>)}</Form.Select><Form.Select value={permission} onChange={(e) => setPermission(e.target.value as Permission)}><option value="read">只读</option><option value="write">可修改</option></Form.Select><Button type="submit">保存授权</Button></form><div className="grant-list">{selected.grants.map((grant) => <div key={grant.id}><span><small>{grant.provider}</small><strong>{grant.project_path}</strong></span><span className={`permission ${grant.permission}`}>{grant.permission === 'write' ? '可修改' : '只读'}</span><Button variant="link" onClick={async () => { await api.deleteGrant(selected.id, grant.id); await load() }}>移除</Button></div>)}</div></>}</>}</section></div>
    <Modal show={showCreate} onHide={() => setShowCreate(false)} centered><form onSubmit={create}><Modal.Header closeButton><Modal.Title>新增审核用户</Modal.Title></Modal.Header><Modal.Body><Form.Group className="mb-3"><Form.Label>用户名</Form.Label><Form.Control value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required /></Form.Group><Form.Group className="mb-3"><Form.Label>显示名称</Form.Label><Form.Control value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} required /></Form.Group><Form.Group className="mb-3"><Form.Label>初始密码（至少 10 位）</Form.Label><Form.Control type="password" minLength={10} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required /></Form.Group><Form.Check type="switch" label="设为管理员" checked={form.is_admin} onChange={(e) => setForm({ ...form, is_admin: e.target.checked })} /></Modal.Body><Modal.Footer><Button variant="outline-secondary" onClick={() => setShowCreate(false)}>取消</Button><Button type="submit">创建用户</Button></Modal.Footer></form></Modal>
  </div>
}

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [checkingSession, setCheckingSession] = useState(true)
  const [repositories, setRepositories] = useState<Repository[]>([])
  const [page, setPage] = useState<Page>(pageFromUrl)
  const [routeVersion, setRouteVersion] = useState(0)
  useEffect(() => { api.me().then(setUser).catch(() => undefined).finally(() => setCheckingSession(false)) }, [])
  useEffect(() => { if (user) api.repositories().then((r) => setRepositories(r.items)) }, [user])
  useEffect(() => {
    function handlePopState() { setPage(pageFromUrl()); setRouteVersion((version) => version + 1) }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])
  function navigate(nextPage: Page) {
    const params = new URLSearchParams(window.location.search)
    params.set('view', nextPage)
    window.history.pushState({}, '', `${window.location.pathname}?${params.toString()}${window.location.hash}`)
    setPage(nextPage)
  }
  if (checkingSession) return <div className="session-loading">正在验证会话…</div>
  if (!user) return <Login onLogin={setUser} />
  const currentPage = page === 'users' && !user.is_admin ? 'issues' : page
  return <div className="app-shell"><nav className="topbar"><div className="wordmark"><span><ShieldCheck size={20} /></span><strong>检视裁定台</strong><small>REVIEW CONTROL</small></div><div className="nav-tabs"><button className={currentPage === 'issues' ? 'active' : ''} onClick={() => navigate('issues')}><Code2 />意见审核</button><button className={currentPage === 'history' ? 'active' : ''} onClick={() => navigate('history')}><History />变更历史</button><button className={currentPage === 'statistics' ? 'active' : ''} onClick={() => navigate('statistics')}><BarChart3 />数据统计</button>{user.is_admin && <button className={currentPage === 'users' ? 'active' : ''} onClick={() => navigate('users')}><Users />角色管理</button>}</div><div className="profile"><span><strong>{user.display_name}</strong><small>{user.is_admin ? '管理员' : '审核者'}</small></span><button aria-label="退出登录" onClick={async () => { await api.logout(); setUser(null) }}><LogOut size={18} /></button></div></nav>{currentPage === 'issues' ? <IssueWorkspace repositories={repositories} routeVersion={routeVersion} /> : currentPage === 'history' ? <HistoryWorkspace repositories={repositories} /> : currentPage === 'statistics' ? <StatisticsWorkspace /> : <UserAdmin repositories={repositories} currentUserId={user.id} />}</div>
}
