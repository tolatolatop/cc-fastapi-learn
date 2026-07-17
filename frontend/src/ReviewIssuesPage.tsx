import { FormEvent, KeyboardEvent, useCallback, useEffect, useState } from 'react'
import {
  Check,
  ChevronRight,
  CircleAlert,
  ExternalLink,
  FileCode,
  GitBranch,
  GitMerge,
  GitPullRequest,
  KeyRound,
  Plus,
  RefreshCw,
  Search,
  Server,
  Settings,
  ShieldCheck,
  Trash2,
  X,
  XCircle,
} from 'lucide-react'
import { api } from './api'
import Pagination from './Pagination'
import type {
  CreateReviewIssuePayload,
  ReviewBatchStatus,
  ReviewIssue,
  ReviewIssueBatch,
  ReviewIssueSeverity,
  ReviewIssueStatistics,
  ReviewIssueVerificationStatus,
} from './types'

const BATCH_STATUS_META: Record<ReviewBatchStatus, { label: string; hint: string }> = {
  collecting: { label: '提取中', hint: '等待录入检视意见' },
  waiting_merge: { label: '等待合入', hint: '问题已归档' },
  verifying: { label: '验证中', hint: '核对合入代码' },
  completed: { label: '已完成', hint: '采纳结果已确认' },
  failed: { label: '失败', hint: '回收流程异常' },
  cancelled: { label: '已取消', hint: '未进入统计' },
}

const SEVERITY_META: Record<ReviewIssueSeverity, { label: string; short: string }> = {
  critical: { label: '严重', short: 'C' },
  high: { label: '高', short: 'H' },
  medium: { label: '中', short: 'M' },
  low: { label: '低', short: 'L' },
  info: { label: '提示', short: 'I' },
}

const VERIFICATION_LABEL: Record<ReviewIssueVerificationStatus, string> = {
  unverified: '待验证',
  accepted: '已接受',
  not_accepted: '未接受',
}

const EMPTY_STATISTICS: ReviewIssueStatistics = {
  batch_total: 0,
  zero_issue_batches: 0,
  batch_status_counts: {
    collecting: 0,
    waiting_merge: 0,
    verifying: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
  },
  issue_total: 0,
  verified_issues: 0,
  accepted_issues: 0,
  acceptance_rate: null,
  verification_status_counts: { unverified: 0, accepted: 0, not_accepted: 0 },
  severity_counts: { critical: 0, high: 0, medium: 0, low: 0, info: 0 },
}

function messageFrom(error: unknown) {
  return error instanceof Error ? error.message : '发生未知错误'
}

function formatDate(value: string | null) {
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

function compactSha(value: string | null) {
  return value ? value.slice(0, 9) : '—'
}

function compactTask(value: string | null) {
  return value ? `TASK-${value.slice(0, 8).toUpperCase()}` : '—'
}

async function loadBatchIssues(batchId: string) {
  const firstPage = await api.listReviewIssues({ batchId, limit: 200 })
  if (firstPage.total <= firstPage.items.length) return firstPage.items
  const remainingPages = Math.ceil((firstPage.total - firstPage.items.length) / 200)
  const responses = await Promise.all(
    Array.from({ length: remainingPages }, (_, index) => api.listReviewIssues({
      batchId,
      offset: (index + 1) * 200,
      limit: 200,
    })),
  )
  return [...firstPage.items, ...responses.flatMap((response) => response.items)]
}

function ReviewBatchBadge({ status }: { status: ReviewBatchStatus }) {
  return <span className={`review-batch-badge review-batch-${status}`}><i />{BATCH_STATUS_META[status].label}</span>
}

interface IssueDraft {
  key: string
  severity: ReviewIssueSeverity
  category: string
  title: string
  description: string
  filePath: string
  lineNumber: string
}

function newIssueDraft(): IssueDraft {
  return {
    key: `${Date.now()}-${Math.random()}`,
    severity: 'medium',
    category: '',
    title: '',
    description: '',
    filePath: '',
    lineNumber: '',
  }
}

interface ReviewEntryModalProps {
  existingBatch?: ReviewIssueBatch
  onClose: () => void
  onSaved: (batch: ReviewIssueBatch) => void
}

function ReviewEntryModal({ existingBatch, onClose, onSaved }: ReviewEntryModalProps) {
  const [provider, setProvider] = useState(existingBatch?.provider || 'gitlab')
  const [instanceUrl, setInstanceUrl] = useState(existingBatch?.instance_url || '')
  const [projectPath, setProjectPath] = useState(existingBatch?.project_path || '')
  const [prNumber, setPrNumber] = useState(existingBatch?.pr_number || '')
  const [prUrl, setPrUrl] = useState(existingBatch?.pr_url || '')
  const [reviewTaskId, setReviewTaskId] = useState(existingBatch?.review_task_id || '')
  const [workflowRunId, setWorkflowRunId] = useState(existingBatch?.review_workflow_run_id || '')
  const [extractTaskId, setExtractTaskId] = useState(existingBatch?.extract_task_id || '')
  const [reviewHeadSha, setReviewHeadSha] = useState(existingBatch?.review_head_sha || '')
  const [issues, setIssues] = useState<IssueDraft[]>([newIssueDraft()])
  const [noIssues, setNoIssues] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [workingBatch, setWorkingBatch] = useState(existingBatch)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, submitting])

  function updateIssue(key: string, values: Partial<IssueDraft>) {
    setIssues((current) => current.map((issue) => issue.key === key ? { ...issue, ...values } : issue))
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      let batch = workingBatch
      if (!batch) {
        batch = await api.createReviewBatch({
          provider: provider.trim(),
          project_path: projectPath.trim(),
          pr_number: prNumber.trim(),
          review_task_id: reviewTaskId.trim(),
          ...(instanceUrl.trim() ? { instance_url: instanceUrl.trim() } : {}),
          ...(prUrl.trim() ? { pr_url: prUrl.trim() } : {}),
          ...(workflowRunId.trim() ? { review_workflow_run_id: workflowRunId.trim() } : {}),
          ...(extractTaskId.trim() ? { extract_task_id: extractTaskId.trim() } : {}),
          ...(reviewHeadSha.trim() ? { review_head_sha: reviewHeadSha.trim() } : {}),
        })
        setWorkingBatch(batch)
      }
      const payload: CreateReviewIssuePayload[] = noIssues ? [] : issues.map((issue) => ({
        severity: issue.severity,
        title: issue.title.trim(),
        description: issue.description.trim(),
        ...(issue.category.trim() ? { category: issue.category.trim() } : {}),
        ...(issue.filePath.trim() ? { file_path: issue.filePath.trim() } : {}),
        ...(issue.lineNumber ? { line_number: Number(issue.lineNumber) } : {}),
      }))
      await api.createReviewIssues(batch.id, payload)
      onSaved(await api.getReviewBatch(batch.id))
    } catch (requestError) {
      setError(messageFrom(requestError))
      setSubmitting(false)
    }
  }

  const validIssues = noIssues || (issues.length > 0 && issues.every((issue) => issue.title.trim() && issue.description.trim()))
  const validBatch = Boolean(workingBatch || (provider.trim() && projectPath.trim() && prNumber.trim() && reviewTaskId.trim()))

  return (
    <div className="modal-backdrop review-modal-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="review-entry-modal" role="dialog" aria-modal="true" aria-labelledby="review-entry-title">
        <div className="review-modal-head">
          <div>
            <p className="eyebrow">REVIEW INTAKE</p>
            <h2 id="review-entry-title">{existingBatch ? '录入提取结果' : '录入一次代码检视'}</h2>
            <p>{existingBatch ? `为 ${existingBatch.project_path} !${existingBatch.pr_number} 补充问题列表。` : '关联原始 Agent 任务，并把检视意见转成可统计的问题。'}</p>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭录入窗口" disabled={submitting}><X size={19} /></button>
        </div>

        <form onSubmit={submit}>
          {!existingBatch && (
            <section className="review-form-section">
              <div className="review-form-section-title"><span>批次</span><small>代码位置与来源任务</small></div>
              <div className="review-form-grid review-form-grid-primary">
                <label className="field">
                  <span>代码平台</span>
                  <select value={provider} onChange={(event) => setProvider(event.target.value)} required>
                    <option value="gitlab">GitLab</option>
                    <option value="github">GitHub</option>
                  </select>
                </label>
                <label className="field review-project-field">
                  <span>代码仓库</span>
                  <input value={projectPath} onChange={(event) => setProjectPath(event.target.value)} placeholder="group/project" required />
                </label>
                <label className="field">
                  <span>PR / MR 编号</span>
                  <input value={prNumber} onChange={(event) => setPrNumber(event.target.value)} placeholder="42" required />
                </label>
                <label className="field review-task-field">
                  <span>原始检视任务 ID</span>
                  <input value={reviewTaskId} onChange={(event) => setReviewTaskId(event.target.value)} placeholder="完整 Agent Task UUID" required />
                </label>
              </div>
            </section>
          )}

          <section className="review-advanced-wrap">
            <button
              type="button"
              className="review-advanced-toggle"
              aria-expanded={advancedOpen}
              onClick={() => setAdvancedOpen((value) => !value)}
            >
              <Settings size={17} />
              <span><strong>高级选项</strong><small>版本、关联任务、平台链接与代码位置</small></span>
              <ChevronRight size={17} className={advancedOpen ? 'is-open' : ''} />
            </button>
            {advancedOpen && (
              <div className="review-advanced-panel">
                {!existingBatch && (
                  <>
                    <div className="review-advanced-panel-head">
                      <strong>批次追溯信息</strong>
                      <span>这些字段便于回看任务和代码版本，不影响问题数量与采纳率。</span>
                    </div>
                    <div className="review-form-grid review-advanced-grid">
                      <label className="field">
                        <span>检视版本 SHA</span>
                        <input value={reviewHeadSha} onChange={(event) => setReviewHeadSha(event.target.value)} placeholder="可选" />
                      </label>
                      <label className="field">
                        <span>提取任务 ID</span>
                        <input value={extractTaskId} onChange={(event) => setExtractTaskId(event.target.value)} placeholder="可选" />
                      </label>
                      <label className="field">
                        <span>Workflow Run ID</span>
                        <input value={workflowRunId} onChange={(event) => setWorkflowRunId(event.target.value)} placeholder="可选" />
                      </label>
                      <label className="field">
                        <span>平台实例</span>
                        <input value={instanceUrl} onChange={(event) => setInstanceUrl(event.target.value)} placeholder="例如 https://gitlab.example.com" />
                      </label>
                      <label className="field review-wide-field">
                        <span>PR 地址</span>
                        <input value={prUrl} onChange={(event) => setPrUrl(event.target.value)} placeholder="具体 PR / MR 页面的完整链接" />
                      </label>
                    </div>
                  </>
                )}
                <div className="review-advanced-issue-note">
                  <FileCode size={15} />
                  <span>已显示问题分类、文件路径和行号字段。</span>
                </div>
              </div>
            )}
          </section>

          <section className="review-form-section review-issues-form-section">
            <div className="review-form-section-title">
              <span>问题</span>
              <div>
                <label className="review-zero-toggle">
                  <input type="checkbox" checked={noIssues} onChange={(event) => setNoIssues(event.target.checked)} />
                  本次未发现问题
                </label>
                {!noIssues && <button type="button" className="review-add-issue" onClick={() => setIssues((current) => [...current, newIssueDraft()])}><Plus size={14} />增加问题</button>}
              </div>
            </div>

            {noIssues ? (
              <div className="review-zero-state"><ShieldCheck size={22} /><div><strong>记录为零问题批次</strong><span>它仍会计入检视批次和零问题统计。</span></div></div>
            ) : (
              <div className="review-issue-editor-list">
                {issues.map((issue, index) => (
                  <article className="review-issue-editor" key={issue.key}>
                    <header>
                      <span>ISSUE {String(index + 1).padStart(2, '0')}</span>
                      {issues.length > 1 && <button type="button" onClick={() => setIssues((current) => current.filter((item) => item.key !== issue.key))} aria-label={`删除问题 ${index + 1}`}><Trash2 size={15} /></button>}
                    </header>
                    <div className={`review-issue-editor-grid ${advancedOpen ? 'has-advanced' : ''}`}>
                      <label className="field">
                        <span>等级</span>
                        <select value={issue.severity} onChange={(event) => updateIssue(issue.key, { severity: event.target.value as ReviewIssueSeverity })}>
                          {Object.entries(SEVERITY_META).map(([value, meta]) => <option value={value} key={value}>{meta.label}</option>)}
                        </select>
                      </label>
                      {advancedOpen && (
                        <label className="field">
                          <span>分类</span>
                          <input value={issue.category} onChange={(event) => updateIssue(issue.key, { category: event.target.value })} placeholder="correctness / security" />
                        </label>
                      )}
                      <label className="field review-editor-title">
                        <span>问题摘要</span>
                        <input value={issue.title} onChange={(event) => updateIssue(issue.key, { title: event.target.value })} placeholder="简洁描述问题" required />
                      </label>
                      <label className="field review-editor-description">
                        <span>完整检视意见</span>
                        <textarea value={issue.description} onChange={(event) => updateIssue(issue.key, { description: event.target.value })} placeholder="说明风险、触发条件和建议修改方式" rows={3} required />
                      </label>
                      {advancedOpen && (
                        <>
                          <label className="field review-editor-path">
                            <span>文件</span>
                            <input value={issue.filePath} onChange={(event) => updateIssue(issue.key, { filePath: event.target.value })} placeholder="src/example.py" />
                          </label>
                          <label className="field">
                            <span>行号</span>
                            <input type="number" min={1} value={issue.lineNumber} onChange={(event) => updateIssue(issue.key, { lineNumber: event.target.value })} placeholder="可选" />
                          </label>
                        </>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>

          {workingBatch && !existingBatch && error && <div className="review-partial-notice"><CircleAlert size={16} /><span>批次已经创建，修正问题数据后可直接重试，不会重复创建批次。</span></div>}
          {error && <div className="inline-error"><CircleAlert size={16} />{error}</div>}

          <div className="review-modal-actions">
            <span>{noIssues ? '将写入 0 条问题' : `将写入 ${issues.length} 条问题`}</span>
            <div>
              <button type="button" className="button button-quiet" onClick={onClose} disabled={submitting}>取消</button>
              <button type="submit" className="button button-primary" disabled={!validBatch || !validIssues || submitting}>
                {submitting ? <RefreshCw size={16} className="spin" /> : <Check size={16} />}
                {submitting ? '正在保存' : '保存检视结果'}
              </button>
            </div>
          </div>
        </form>
      </section>
    </div>
  )
}

interface ReviewBatchDrawerProps {
  batch: ReviewIssueBatch
  onClose: () => void
  onChanged: (batch: ReviewIssueBatch) => void
  onOpenTask: (taskId: string) => void
  onContinueCollection: (batch: ReviewIssueBatch) => void
}

function ReviewBatchDrawer({ batch, onClose, onChanged, onOpenTask, onContinueCollection }: ReviewBatchDrawerProps) {
  const [detail, setDetail] = useState(batch)
  const [issues, setIssues] = useState<ReviewIssue[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [mergedSha, setMergedSha] = useState(batch.merged_sha || '')
  const [verifyTaskId, setVerifyTaskId] = useState(batch.verify_task_id || '')
  const [decision, setDecision] = useState<{ issueId: string; status: 'accepted' | 'not_accepted' } | null>(null)
  const [decisionNote, setDecisionNote] = useState('')
  const [saving, setSaving] = useState(false)

  const loadDetail = useCallback(async () => {
    setLoading(true)
    try {
      const [nextBatch, nextIssues] = await Promise.all([
        api.getReviewBatch(batch.id),
        loadBatchIssues(batch.id),
      ])
      setDetail(nextBatch)
      setMergedSha(nextBatch.merged_sha || '')
      setVerifyTaskId(nextBatch.verify_task_id || '')
      setIssues(nextIssues.sort((left, right) => left.issue_no - right.issue_no))
      setError('')
    } catch (requestError) {
      setError(messageFrom(requestError))
    } finally {
      setLoading(false)
    }
  }, [batch.id])

  useEffect(() => {
    loadDetail()
  }, [loadDetail])

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function beginVerification(event: FormEvent) {
    event.preventDefault()
    if (!mergedSha.trim()) return
    setSaving(true)
    setError('')
    try {
      const updated = await api.updateReviewBatch(detail.id, {
        status: detail.issue_count === 0 ? 'completed' : 'verifying',
        merged_sha: mergedSha.trim(),
        ...(verifyTaskId.trim() ? { verify_task_id: verifyTaskId.trim() } : {}),
      })
      setDetail(updated)
      onChanged(updated)
    } catch (requestError) {
      setError(messageFrom(requestError))
    } finally {
      setSaving(false)
    }
  }

  async function saveDecision(event: FormEvent) {
    event.preventDefault()
    if (!decision) return
    setSaving(true)
    setError('')
    try {
      await api.updateReviewIssue(decision.issueId, {
        status: decision.status,
        ...(decisionNote.trim() ? { note: decisionNote.trim() } : {}),
      })
      setDecision(null)
      setDecisionNote('')
      const [nextBatch, nextIssues] = await Promise.all([
        api.getReviewBatch(detail.id),
        loadBatchIssues(detail.id),
      ])
      setDetail(nextBatch)
      setIssues(nextIssues.sort((left, right) => left.issue_no - right.issue_no))
      onChanged(nextBatch)
    } catch (requestError) {
      setError(messageFrom(requestError))
    } finally {
      setSaving(false)
    }
  }

  const accepted = issues.filter((issue) => issue.verification_status === 'accepted').length
  const rejected = issues.filter((issue) => issue.verification_status === 'not_accepted').length

  return (
    <div className="drawer-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside className="detail-drawer review-detail-drawer" role="dialog" aria-modal="true" aria-labelledby="review-detail-title">
        <div className="drawer-head">
          <button className="icon-button" onClick={onClose} aria-label="关闭检视详情"><X size={19} /></button>
          <span className="drawer-record-id">BATCH-{detail.id.slice(0, 8).toUpperCase()}</span>
        </div>

        <div className="review-detail-intro">
          <div className="review-detail-kicker"><ReviewBatchBadge status={detail.status} /><span>{BATCH_STATUS_META[detail.status].hint}</span></div>
          <h2 id="review-detail-title">{detail.project_path}</h2>
          <div className="review-pr-line">
            <span><GitPullRequest size={14} />!{detail.pr_number}</span>
            {detail.pr_url && <a href={detail.pr_url} target="_blank" rel="noreferrer">打开代码平台<ExternalLink size={13} /></a>}
          </div>
        </div>

        <div className="review-detail-scroll">
          <section className="review-result-strip">
            <div><strong>{detail.issue_count}</strong><span>检视问题</span></div>
            <div><strong className="accepted">{accepted}</strong><span>已接受</span></div>
            <div><strong>{rejected}</strong><span>未接受</span></div>
          </section>

          <section className="detail-section">
            <h3>数据来源</h3>
            <dl className="detail-grid review-detail-grid">
              <div><dt>检视版本</dt><dd>{compactSha(detail.review_head_sha)}</dd></div>
              <div><dt>合入版本</dt><dd>{compactSha(detail.merged_sha)}</dd></div>
              <div><dt>创建时间</dt><dd>{formatDate(detail.created_at)}</dd></div>
              <div><dt>完成时间</dt><dd>{formatDate(detail.verified_at)}</dd></div>
            </dl>
            <div className="review-task-links">
              {[
                ['检视', detail.review_task_id],
                ['提取', detail.extract_task_id],
                ['验证', detail.verify_task_id],
              ].map(([label, taskId]) => taskId && (
                <button key={label} onClick={() => onOpenTask(taskId)}><span>{label}任务</span><strong>{compactTask(taskId)}</strong><ExternalLink size={12} /></button>
              ))}
            </div>
          </section>

          {detail.status === 'collecting' && (
            <section className="review-next-step">
              <FileCode size={20} />
              <div><strong>等待问题提取结果</strong><p>录入问题列表后，批次会进入等待合入阶段。</p></div>
              <button className="button button-primary" onClick={() => onContinueCollection(detail)}><Plus size={15} />录入问题</button>
            </section>
          )}

          {detail.status === 'waiting_merge' && (
            <form className="review-merge-form" onSubmit={beginVerification}>
              <div className="review-merge-form-title"><GitMerge size={19} /><div><strong>{detail.issue_count ? '开始合入后验证' : '确认零问题批次已合入'}</strong><span>填写最终代码版本，建立本次验证基线。</span></div></div>
              <label className="field"><span>合入版本 SHA</span><input value={mergedSha} onChange={(event) => setMergedSha(event.target.value)} placeholder="merged commit SHA" required /></label>
              <label className="field"><span>验证任务 ID</span><input value={verifyTaskId} onChange={(event) => setVerifyTaskId(event.target.value)} placeholder="可选" /></label>
              <button className="button button-primary" type="submit" disabled={!mergedSha.trim() || saving}>{saving ? <RefreshCw className="spin" size={15} /> : <GitMerge size={15} />}{detail.issue_count ? '进入验证' : '完成批次'}</button>
            </form>
          )}

          {error && <div className="inline-error review-detail-error"><CircleAlert size={16} />{error}</div>}

          <section className="detail-section review-issue-detail-section">
            <div className="review-issues-heading"><h3>问题列表</h3><span>{issues.length} 条</span></div>
            {loading ? (
              <div className="review-detail-loading"><RefreshCw size={20} className="spin" />正在读取问题…</div>
            ) : issues.length === 0 ? (
              <div className="review-detail-empty"><ShieldCheck size={22} /><strong>本次检视没有提取到问题</strong></div>
            ) : (
              <div className="review-finding-list">
                {issues.map((issue) => (
                  <article className={`review-finding review-finding-${issue.verification_status}`} key={issue.id}>
                    <div className="review-finding-topline">
                      <span className={`review-severity review-severity-${issue.severity}`}><b>{SEVERITY_META[issue.severity].short}</b>{SEVERITY_META[issue.severity].label}</span>
                      <span className={`review-verification review-verification-${issue.verification_status}`}><i />{VERIFICATION_LABEL[issue.verification_status]}</span>
                    </div>
                    <h4>{issue.title}</h4>
                    <p>{issue.description}</p>
                    <footer>
                      <span>{issue.file_path ? <><FileCode size={12} />{issue.file_path}{issue.line_number ? `:${issue.line_number}` : ''}</> : '未关联文件'}</span>
                      {issue.category && <em>{issue.category}</em>}
                    </footer>
                    {issue.verification_note && <div className="review-verification-note"><span>验证依据</span>{issue.verification_note}</div>}
                    {detail.status === 'verifying' && issue.verification_status === 'unverified' && decision?.issueId !== issue.id && (
                      <div className="review-decision-actions">
                        <button onClick={() => { setDecision({ issueId: issue.id, status: 'accepted' }); setDecisionNote('') }}><Check size={14} />已修复，接受</button>
                        <button onClick={() => { setDecision({ issueId: issue.id, status: 'not_accepted' }); setDecisionNote('') }}><XCircle size={14} />未发现修复</button>
                      </div>
                    )}
                    {decision?.issueId === issue.id && (
                      <form className={`review-decision-form decision-${decision.status}`} onSubmit={saveDecision}>
                        <strong>{decision.status === 'accepted' ? '记录为已接受' : '记录为未接受'}</strong>
                        <textarea value={decisionNote} onChange={(event) => setDecisionNote(event.target.value)} rows={2} placeholder="可选：填写判断依据" autoFocus />
                        <div><button type="button" onClick={() => setDecision(null)}>取消</button><button type="submit" disabled={saving}>{saving ? '正在保存' : '确认结论'}</button></div>
                      </form>
                    )}
                  </article>
                ))}
              </div>
            )}
          </section>
        </div>
      </aside>
    </div>
  )
}

interface ReviewIssuesPageProps {
  onOpenSettings: () => void
  onOpenTask: (taskId: string) => void
}

export default function ReviewIssuesPage({ onOpenSettings, onOpenTask }: ReviewIssuesPageProps) {
  const [batches, setBatches] = useState<ReviewIssueBatch[]>([])
  const [total, setTotal] = useState(0)
  const [statistics, setStatistics] = useState<ReviewIssueStatistics>(EMPTY_STATISTICS)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [projectSearch, setProjectSearch] = useState('')
  const [prSearch, setPrSearch] = useState('')
  const [debouncedProject, setDebouncedProject] = useState('')
  const [debouncedPr, setDebouncedPr] = useState('')
  const [statusFilter, setStatusFilter] = useState<ReviewBatchStatus | 'all'>('all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [selected, setSelected] = useState<ReviewIssueBatch | null>(null)
  const [entryOpen, setEntryOpen] = useState(false)
  const [collectingBatch, setCollectingBatch] = useState<ReviewIssueBatch | null>(null)
  const [notice, setNotice] = useState('')

  const loadData = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true)
    else setLoading(true)
    try {
      const [batchResponse, summaryResponse] = await Promise.all([
        api.listReviewBatches({
          offset: (page - 1) * pageSize,
          limit: pageSize,
          projectPath: debouncedProject || undefined,
          prNumber: debouncedPr || undefined,
          statuses: statusFilter === 'all' ? [] : [statusFilter],
        }),
        api.getReviewStatistics({
          projectPath: debouncedProject || undefined,
          prNumber: debouncedPr || undefined,
        }),
      ])
      setBatches(batchResponse.items)
      setTotal(batchResponse.total)
      setStatistics(summaryResponse)
      setError('')
      const lastPage = Math.max(1, Math.ceil(batchResponse.total / pageSize))
      if (page > lastPage) setPage(lastPage)
    } catch (requestError) {
      setError(messageFrom(requestError))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [debouncedPr, debouncedProject, page, pageSize, statusFilter])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedProject(projectSearch.trim())
      setDebouncedPr(prSearch.trim())
      setPage(1)
    }, 300)
    return () => window.clearTimeout(timer)
  }, [prSearch, projectSearch])

  useEffect(() => {
    loadData()
    const timer = window.setInterval(() => {
      if (!document.hidden) loadData(true)
    }, 15000)
    return () => window.clearInterval(timer)
  }, [loadData])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 3500)
    return () => window.clearTimeout(timer)
  }, [notice])

  function handleRowKey(event: KeyboardEvent<HTMLTableRowElement>, batch: ReviewIssueBatch) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      setSelected(batch)
    }
  }

  async function handleChanged(batch: ReviewIssueBatch) {
    setSelected(batch)
    setNotice(batch.status === 'completed' ? '检视批次已经完成验证' : '检视数据已更新')
    await loadData(true)
  }

  const acceptancePercent = statistics.acceptance_rate === null ? '—' : `${Math.round(statistics.acceptance_rate * 100)}%`
  const verificationCoverage = statistics.issue_total ? Math.round((statistics.verified_issues / statistics.issue_total) * 100) : 0
  const acceptedCoverage = statistics.issue_total ? Math.round((statistics.accepted_issues / statistics.issue_total) * 100) : 0

  return (
    <>
      <section className="page-heading review-page-heading">
        <div>
          <p className="eyebrow">CODE REVIEW / 采纳观测</p>
          <h1>检视统计</h1>
          <p>观察检视意见是否真正进入了合入代码。</p>
        </div>
        <button className="button button-primary review-entry-button" onClick={() => setEntryOpen(true)}><Plus size={17} />录入检视</button>
      </section>

      <section className="review-signal-rail" aria-label="检视问题采纳轨道">
        <div className="review-signal-label">
          <span><i />REVIEW SIGNAL</span>
          <strong>{statistics.batch_total}</strong>
          <small>检视批次</small>
        </div>
        <div className="review-signal-route">
          <div className="review-signal-line"><i className="verified" style={{ width: `${verificationCoverage}%` }} /><i className="accepted" style={{ width: `${acceptedCoverage}%` }} /></div>
          <div className="review-signal-stop is-found"><span><FileCode size={15} /></span><div><strong>{statistics.issue_total}</strong><small>提取问题</small></div></div>
          <div className={`review-signal-stop ${statistics.verified_issues ? 'is-verified' : ''}`}><span><GitMerge size={15} /></span><div><strong>{statistics.verified_issues}</strong><small>完成验证</small></div></div>
          <div className={`review-signal-stop ${statistics.accepted_issues ? 'is-accepted' : ''}`}><span><Check size={15} /></span><div><strong>{statistics.accepted_issues}</strong><small>确认接受</small></div></div>
        </div>
        <div className="review-rate-block">
          <span>采纳率</span>
          <strong>{acceptancePercent}</strong>
          <small>{statistics.verified_issues ? `${statistics.accepted_issues} / ${statistics.verified_issues} 条已验证意见` : '等待合入后验证'}</small>
        </div>
      </section>

      <section className="review-pulse-strip" aria-label="检视流程摘要">
        <div><span className="pulse-dot waiting" /><strong>{statistics.batch_status_counts.waiting_merge}</strong><span>等待合入</span></div>
        <div><span className="pulse-dot verifying" /><strong>{statistics.batch_status_counts.verifying}</strong><span>正在验证</span></div>
        <div><span className="pulse-dot complete" /><strong>{statistics.batch_status_counts.completed}</strong><span>完成批次</span></div>
        <div><span className="pulse-dot zero" /><strong>{statistics.zero_issue_batches}</strong><span>零问题批次</span></div>
        <div className="review-severity-mini"><span>等级分布</span>{(['critical', 'high', 'medium', 'low', 'info'] as ReviewIssueSeverity[]).map((severity) => <i className={`severity-${severity}`} key={severity} style={{ flexGrow: statistics.severity_counts[severity] || 0 }} title={`${SEVERITY_META[severity].label}：${statistics.severity_counts[severity]}`} />)}</div>
      </section>

      <section className="review-batch-panel">
        <div className="review-panel-head">
          <div><p className="eyebrow">COLLECTION LEDGER</p><h2>回收批次</h2><span>找到 {total.toLocaleString('zh-CN')} 条记录</span></div>
          <div className="review-filter-tools">
            <label className="review-search-field"><Search size={16} /><input value={projectSearch} onChange={(event) => setProjectSearch(event.target.value)} placeholder="仓库 group/project" aria-label="按仓库筛选" />{projectSearch && <button onClick={() => setProjectSearch('')} aria-label="清除仓库筛选"><X size={13} /></button>}</label>
            <label className="review-pr-filter"><GitPullRequest size={15} /><input value={prSearch} onChange={(event) => setPrSearch(event.target.value)} placeholder="PR 编号" aria-label="按 PR 编号筛选" /></label>
            <label className="review-status-filter"><select value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value as ReviewBatchStatus | 'all'); setPage(1) }} aria-label="按回收状态筛选"><option value="all">全部状态</option>{Object.entries(BATCH_STATUS_META).map(([value, meta]) => <option value={value} key={value}>{meta.label}</option>)}</select></label>
            <button className="icon-button" onClick={() => loadData(true)} aria-label="刷新检视统计"><RefreshCw size={16} className={refreshing ? 'spin' : ''} /></button>
          </div>
        </div>

        <div className="review-batch-list-wrap">
          {loading ? (
            <div className="state-message"><RefreshCw size={24} className="spin" /><strong>正在读取检视统计</strong><p>加载回收批次和采纳结果…</p></div>
          ) : error ? (
            <div className="state-message error-state"><CircleAlert size={26} /><strong>无法读取检视数据</strong><p>{error}</p><div>{error === 'invalid api token' && <button className="button button-quiet" onClick={onOpenSettings}><KeyRound size={16} />填写 Token</button>}<button className="button button-primary" onClick={() => loadData()}><RefreshCw size={16} />重试连接</button></div></div>
          ) : batches.length === 0 ? (
            <div className="state-message review-empty-state"><ShieldCheck size={27} /><strong>{statistics.batch_total ? '没有匹配的回收批次' : '还没有检视统计数据'}</strong><p>{statistics.batch_total ? '调整仓库、PR 或状态筛选后再试。' : '录入第一份检视结果，开始观察问题采纳情况。'}</p>{!statistics.batch_total && <button className="button button-primary" onClick={() => setEntryOpen(true)}><Plus size={16} />录入检视</button>}</div>
          ) : (
            <table className="review-batch-table">
              <thead><tr><th>仓库 / PR</th><th>回收阶段</th><th>问题</th><th>检视版本</th><th>创建时间</th><th>来源任务</th><th><span className="sr-only">操作</span></th></tr></thead>
              <tbody>{batches.map((batch) => (
                <tr key={batch.id} tabIndex={0} onClick={() => setSelected(batch)} onKeyDown={(event) => handleRowKey(event, batch)}>
                  <td><div className="review-pr-subject"><strong>{batch.project_path}</strong><span><GitPullRequest size={12} />!{batch.pr_number}<i />{batch.provider}</span></div></td>
                  <td><div className="review-stage-cell"><ReviewBatchBadge status={batch.status} /><span>{BATCH_STATUS_META[batch.status].hint}</span></div></td>
                  <td><span className="review-issue-count">{batch.issue_count}</span></td>
                  <td><code className="review-sha"><GitBranch size={12} />{compactSha(batch.review_head_sha)}</code></td>
                  <td><span className="date-cell">{formatDate(batch.created_at)}</span></td>
                  <td><button className="review-task-link" onClick={(event) => { event.stopPropagation(); onOpenTask(batch.review_task_id) }}>{compactTask(batch.review_task_id)}<ExternalLink size={11} /></button></td>
                  <td><button className="row-action" onClick={(event) => { event.stopPropagation(); setSelected(batch) }} aria-label={`查看 ${batch.project_path} PR ${batch.pr_number}`}><ChevronRight size={17} /></button></td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </div>

        {!loading && !error && batches.length > 0 && (
          <div className="panel-footer"><Pagination page={page} pageSize={pageSize} total={total} itemLabel="检视批次" onPageChange={setPage} onPageSizeChange={(value) => { setPageSize(value); setPage(1) }} /><span className="panel-source"><Server size={13} />数据来自检视统计 API</span></div>
        )}
      </section>

      {entryOpen && <ReviewEntryModal onClose={() => setEntryOpen(false)} onSaved={(batch) => { setEntryOpen(false); setSelected(batch); setNotice('检视结果已录入'); loadData(true) }} />}
      {collectingBatch && <ReviewEntryModal existingBatch={collectingBatch} onClose={() => setCollectingBatch(null)} onSaved={(batch) => { setCollectingBatch(null); setSelected(batch); setNotice('问题列表已录入'); loadData(true) }} />}
      {selected && <ReviewBatchDrawer batch={selected} onClose={() => setSelected(null)} onChanged={handleChanged} onOpenTask={onOpenTask} onContinueCollection={(batch) => { setSelected(null); setCollectingBatch(batch) }} />}
      {notice && <div className="toast"><Check size={17} />{notice}</div>}
    </>
  )
}
