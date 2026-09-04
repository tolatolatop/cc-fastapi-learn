import { FormEvent, useCallback, useEffect, useState } from 'react'
import { ArrowRight, BarChart3, Check, Clock3, Code2, HelpCircle, History, LogOut, Search, ShieldCheck, Users, X } from 'lucide-react'
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

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
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

function DecisionModal({ issue, onClose, onSaved }: { issue: Issue | null; onClose: () => void; onSaved: (issue: Issue) => void }) {
  const [status, setStatus] = useState<IssueStatus>('accepted')
  const [reasonCode, setReasonCode] = useState<DecisionReason | ''>('')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (issue) { setStatus(issue.status); setReasonCode(issue.reason_code || ''); setNote(issue.note || ''); setError('') } }, [issue])
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!issue) return
    if (status === 'not_accepted' && (!reasonCode || !note.trim())) { setError('拒绝时必须选择原因分类并填写详细理由'); return }
    if (status === 'needs_info' && !note.trim()) { setError('需要补充信息时必须填写说明'); return }
    setBusy(true); setError('')
    try { onSaved(await api.updateStatus(issue, status, status === 'not_accepted' ? reasonCode || null : null, note.trim() || null)); onClose() }
    catch (reason) { setError((reason as Error).message) } finally { setBusy(false) }
  }
  return <Modal show={Boolean(issue)} onHide={onClose} centered>
    <form onSubmit={submit}><Modal.Header closeButton><Modal.Title>裁定检视意见</Modal.Title></Modal.Header><Modal.Body>
      <p className="decision-title">{issue?.title}</p>
      <Form.Label>裁定结果</Form.Label><div className="decision-options">
        {(['unverified', 'accepted', 'not_accepted', 'needs_info'] as IssueStatus[]).map((value) => <button key={value} className={`decision-option ${status === value ? 'active' : ''} ${statusMeta[value].className}`} type="button" onClick={() => setStatus(value)}>{value === 'accepted' ? <Check /> : value === 'not_accepted' ? <X /> : value === 'needs_info' ? <HelpCircle /> : <Clock3 />}{statusMeta[value].label}</button>)}
      </div>
      {status === 'not_accepted' && <Form.Group className="mb-3"><Form.Label>拒绝原因分类（必填）</Form.Label><Form.Select value={reasonCode} onChange={(event) => setReasonCode(event.target.value as DecisionReason | '')} required><option value="">选择原因</option>{Object.entries(reasonMeta).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</Form.Select></Form.Group>}
      <Form.Group><Form.Label>{status === 'not_accepted' ? '拒绝理由详情（必填）' : status === 'needs_info' ? '待补充内容（必填）' : '裁定说明（可选）'}</Form.Label><Form.Control as="textarea" rows={4} value={note} onChange={(e) => setNote(e.target.value)} placeholder={status === 'not_accepted' ? '说明不接受这条意见的事实依据，便于后续追溯…' : status === 'needs_info' ? '写明需要谁补充哪些事实或上下文…' : '补充判断依据…'} /></Form.Group>
      {error && <div className="form-error">{error}</div>}
    </Modal.Body><Modal.Footer><Button variant="outline-secondary" onClick={onClose}>取消</Button><Button type="submit" disabled={busy}>{busy ? '保存中…' : '保存裁定'}</Button></Modal.Footer></form>
  </Modal>
}

function IssueWorkspace({ repositories }: { repositories: Repository[] }) {
  const [selected, setSelected] = useState<Repository | null>(repositories[0] || null)
  const [pullRequests, setPullRequests] = useState<PullRequest[]>([])
  const [selectedPr, setSelectedPr] = useState<PullRequest | null>(null)
  const [issues, setIssues] = useState<Issue[]>([])
  const [status, setStatus] = useState('')
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [decision, setDecision] = useState<Issue | null>(null)
  useEffect(() => {
    if (!selected) return
    setSelectedPr(null)
    api.pullRequests(selected)
      .then((result) => {
        setPullRequests(result.items)
        setSelectedPr(result.items[0] || null)
      })
      .catch((reason) => setError((reason as Error).message))
  }, [selected])
  const load = useCallback(async () => {
    if (!selected || !selectedPr) { setIssues([]); return }
    setLoading(true); setError('')
    try { setIssues((await api.issues(selected, selectedPr.pr_number, status, query)).items) } catch (reason) { setError((reason as Error).message) } finally { setLoading(false) }
  }, [selected, selectedPr, status, query])
  useEffect(() => { void load() }, [load])
  useEffect(() => { if (repositories.length && !selected) setSelected(repositories[0]) }, [repositories, selected])
  return <div className="workspace-grid">
    <aside className="repo-rail"><p className="rail-label">授权仓库</p>{repositories.map((repo) => <button key={`${repo.provider}/${repo.project_path}`} className={selected?.provider === repo.provider && selected.project_path === repo.project_path ? 'active' : ''} onClick={() => setSelected(repo)}><span className="provider">{repo.provider}</span><strong>{repo.project_path}</strong><small>{repo.pending_total} 条待裁定 · {repo.permission === 'write' ? '可修改' : '只读'}</small></button>)}</aside>
    <section className="review-content">
      {selected ? <><header className="content-header"><div><p className="eyebrow">{selected.provider} / REPOSITORY</p><h1>{selected.project_path}</h1><span className={`permission ${selected.permission}`}>{selected.permission === 'write' ? '可修改' : '只读'}</span></div>{selectedPr && <div className="status-tally"><span><b>{selectedPr.issue_total}</b>全部意见</span><span><b>{selectedPr.reviewed_total}</b>已裁定</span><span><b>{selectedPr.pending_total}</b>待裁定</span></div>}</header>
      <div className="pr-strip">{pullRequests.map((pr) => <button key={pr.pr_number} className={selectedPr?.pr_number === pr.pr_number ? 'active' : ''} onClick={() => setSelectedPr(pr)}><span>PR / MR #{pr.pr_number}</span><strong className={`completion ${pr.completion_status}`}>{completionMeta[pr.completion_status]}</strong><small>{pr.reviewed_total}/{pr.issue_total} 条已裁定</small></button>)}</div>
      {selectedPr && <div className="pr-heading"><div><span>当前变更请求</span><strong>#{selectedPr.pr_number}</strong>{selectedPr.pr_url && <a href={selectedPr.pr_url} target="_blank" rel="noreferrer">打开代码平台</a>}</div><span className={`completion ${selectedPr.completion_status}`}>{completionMeta[selectedPr.completion_status]}</span></div>}
      <div className="filters"><label><Search size={17} /><Form.Control value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索标题、说明或文件" /></label><Form.Select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">全部状态</option><option value="unverified">待裁定</option><option value="accepted">接受</option><option value="not_accepted">拒绝</option><option value="needs_info">待补充</option></Form.Select></div>
      {error && <div className="form-error">{error}</div>}{!selectedPr ? <div className="empty-state">该仓库还没有 PR/MR 检视记录</div> : loading ? <div className="empty-state">正在读取检视意见…</div> : !issues.length ? <div className="empty-state">{selectedPr.completion_status === 'no_issues' ? '该 PR/MR 已检视完毕，没有发现问题' : '当前筛选下没有检视意见'}</div> : <div className="issue-list">{issues.map((issue) => <article className="issue-card" key={issue.id}>
        <div className={`severity-stripe ${issue.severity}`} /><div className="issue-main"><div className="issue-kicker"><span className={`severity ${issue.severity}`}>{issue.severity}</span><span>PR #{issue.pr_number}</span>{issue.category && <span>{issue.category}</span>}</div><h2>{issue.title}</h2><p>{issue.description}</p>{issue.file_path && <span className="file-location">{issue.file_path}{issue.line_number ? `:${issue.line_number}` : ''}</span>}<div className="issue-context"><span>自动修复验证：{issue.verification_status === 'accepted' ? '通过' : issue.verification_status === 'not_accepted' ? '未通过' : '未验证'}</span><span>检视版本：{issue.review_head_sha?.slice(0, 10) || '未知'}</span><span>合并版本：{issue.merged_sha?.slice(0, 10) || '未合并'}</span><span>批次：{issue.batch_status} · {formatTime(issue.batch_created_at)}</span><span>提取：{issue.batch_extracted_at ? formatTime(issue.batch_extracted_at) : '进行中'}</span></div></div>
        <div className="issue-actions"><span className={`status ${statusMeta[issue.status].className}`}>{statusMeta[issue.status].label}</span>{issue.reason_code && <span className="reason-chip">{reasonMeta[issue.reason_code]}</span>}{issue.note && <p className="issue-note">{issue.note}</p>}{issue.decided_by_name && <small>由 {issue.decided_by_name} · {issue.decided_at ? formatTime(issue.decided_at) : ''}</small>}{selected.permission === 'write' && <div><Button size="sm" onClick={() => setDecision(issue)}>裁定</Button></div>}</div>
      </article>)}</div>}</> : <div className="empty-state">尚未分配仓库权限</div>}
    </section>
    <DecisionModal issue={decision} onClose={() => setDecision(null)} onSaved={async (updated) => { setIssues((list) => list.map((item) => item.id === updated.id ? updated : item)); if (selected) { const result = await api.pullRequests(selected); setPullRequests(result.items); setSelectedPr(result.items.find((pr) => pr.pr_number === updated.pr_number) || null) } }} />
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
  const [page, setPage] = useState<'issues' | 'history' | 'statistics' | 'users'>('issues')
  useEffect(() => { api.me().then(setUser).catch(() => undefined).finally(() => setCheckingSession(false)) }, [])
  useEffect(() => { if (user) api.repositories().then((r) => setRepositories(r.items)) }, [user])
  if (checkingSession) return <div className="session-loading">正在验证会话…</div>
  if (!user) return <Login onLogin={setUser} />
  return <div className="app-shell"><nav className="topbar"><div className="wordmark"><span><ShieldCheck size={20} /></span><strong>检视裁定台</strong><small>REVIEW CONTROL</small></div><div className="nav-tabs"><button className={page === 'issues' ? 'active' : ''} onClick={() => setPage('issues')}><Code2 />意见审核</button><button className={page === 'history' ? 'active' : ''} onClick={() => setPage('history')}><History />变更历史</button><button className={page === 'statistics' ? 'active' : ''} onClick={() => setPage('statistics')}><BarChart3 />数据统计</button>{user.is_admin && <button className={page === 'users' ? 'active' : ''} onClick={() => setPage('users')}><Users />角色管理</button>}</div><div className="profile"><span><strong>{user.display_name}</strong><small>{user.is_admin ? '管理员' : '审核者'}</small></span><button aria-label="退出登录" onClick={async () => { await api.logout(); setUser(null) }}><LogOut size={18} /></button></div></nav>{page === 'issues' ? <IssueWorkspace repositories={repositories} /> : page === 'history' ? <HistoryWorkspace repositories={repositories} /> : page === 'statistics' ? <StatisticsWorkspace /> : <UserAdmin repositories={repositories} currentUserId={user.id} />}</div>
}
