import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import {
  Check,
  CircleAlert,
  ExternalLink,
  GitBranch,
  KeyRound,
  LibraryBig,
  RefreshCw,
  Search,
  Server,
  Tags,
  Webhook,
  X,
} from 'lucide-react'
import { Button, Form, Modal, Table } from 'react-bootstrap'
import { api } from './api'
import Pagination from './Pagination'
import type { RepositoryOverviewItem, RepositoryOverviewResponse } from './types'

const EMPTY_OVERVIEW: RepositoryOverviewResponse = {
  items: [],
  total: 0,
  summary: {
    repository_total: 0,
    review_total: 0,
    issue_total: 0,
    accepted_issues: 0,
    unhandled_issues: 0,
    pending_issues: 0,
    providers: [],
    tags: [],
  },
}

function messageFrom(error: unknown) {
  return error instanceof Error ? error.message : '发生未知错误'
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function parseTags(value: string) {
  const tags: string[] = []
  const seen = new Set<string>()
  value.split(/[\n,，]+/).forEach((item) => {
    const tag = item.trim()
    const key = tag.toLocaleLowerCase()
    if (tag && !seen.has(key)) {
      seen.add(key)
      tags.push(tag)
    }
  })
  return tags
}

function IssueBalance({ repository }: { repository: RepositoryOverviewItem }) {
  const statistics = repository.review_statistics
  const denominator = Math.max(1, statistics.issue_total)
  return (
    <div className="repository-issue-balance">
      <div className="repository-issue-counts">
        <span><b>{statistics.issue_total}</b>发现</span>
        <span className="accepted"><b>{statistics.accepted_issues}</b>确认</span>
        <span className="unhandled"><b>{statistics.unhandled_issues}</b>未处理</span>
        <span className="pending"><b>{statistics.pending_issues}</b>待确认</span>
      </div>
      <div className="repository-balance-track" aria-label={`${statistics.issue_total} 个问题`}>
        <i className="accepted" style={{ width: `${statistics.accepted_issues / denominator * 100}%` }} />
        <i className="unhandled" style={{ width: `${statistics.unhandled_issues / denominator * 100}%` }} />
        <i className="pending" style={{ width: `${statistics.pending_issues / denominator * 100}%` }} />
      </div>
    </div>
  )
}

interface TagManagerModalProps {
  repositories: RepositoryOverviewItem[]
  availableTags: string[]
  bulk: boolean
  onClose: () => void
  onSaved: (message: string) => void
}

function TagManagerModal({ repositories, availableTags, bulk, onClose, onSaved }: TagManagerModalProps) {
  const first = repositories[0]
  const [selectedTags, setSelectedTags] = useState(() => new Set(first?.tags || []))
  const [addTags, setAddTags] = useState<Set<string>>(new Set())
  const [removeTags, setRemoveTags] = useState<Set<string>>(new Set())
  const [customTags, setCustomTags] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const custom = useMemo(() => parseTags(customTags), [customTags])
  const allSingleTags = useMemo(
    () => [...new Set([...availableTags, ...(first?.tags || [])])].sort((left, right) => left.localeCompare(right, 'zh-CN')),
    [availableTags, first],
  )

  function toggleSingle(tag: string) {
    setSelectedTags((current) => {
      const next = new Set(current)
      if (next.has(tag)) next.delete(tag)
      else next.add(tag)
      return next
    })
  }

  function toggleBulk(tag: string, operation: 'add' | 'remove') {
    const target = operation === 'add' ? setAddTags : setRemoveTags
    const opposite = operation === 'add' ? setRemoveTags : setAddTags
    target((current) => {
      const next = new Set(current)
      if (next.has(tag)) next.delete(tag)
      else next.add(tag)
      return next
    })
    opposite((current) => {
      const next = new Set(current)
      next.delete(tag)
      return next
    })
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      if (bulk) {
        const additions = [...new Set([...addTags, ...custom])]
        await api.bulkUpdateRepositoryTags({
          repository_ids: repositories.map((repository) => repository.id),
          add_tags: additions,
          remove_tags: [...removeTags],
        })
        onSaved(`已更新 ${repositories.length} 个仓库的 Tags`)
      } else {
        const nextTags = [...new Set([...selectedTags, ...custom])]
        await api.replaceRepositoryTags(first.id, nextTags)
        onSaved(`已更新 ${first.project_path} 的 Tags`)
      }
    } catch (requestError) {
      setError(messageFrom(requestError))
      setSubmitting(false)
    }
  }

  const currentTags = new Set(first?.tags || [])
  const singleAdditions = [...selectedTags, ...custom].filter((tag) => !currentTags.has(tag))
  const singleRemovals = [...currentTags].filter((tag) => !selectedTags.has(tag))
  const bulkAdditions = [...new Set([...addTags, ...custom])]
  const canSave = bulk
    ? bulkAdditions.length > 0 || removeTags.size > 0
    : singleAdditions.length > 0 || singleRemovals.length > 0

  return (
    <Modal show centered size="lg" onHide={onClose} backdrop={submitting ? 'static' : true} keyboard={!submitting} className="repository-tag-modal" aria-labelledby="repository-tag-modal-title">
      <form onSubmit={submit}>
        <Modal.Header closeButton={!submitting}>
          <div>
            <p className="eyebrow">TAG WORKBENCH</p>
            <Modal.Title id="repository-tag-modal-title">{bulk ? '批量管理 Tags' : '管理仓库 Tags'}</Modal.Title>
            <p>{bulk ? `将同时更新 ${repositories.length} 个已选仓库。` : `${first.provider} / ${first.project_path}`}</p>
          </div>
        </Modal.Header>
        <Modal.Body>
          {bulk ? (
            <div className="repository-bulk-tag-grid">
              <section>
                <div className="repository-tag-section-title"><span className="add">新增</span><strong>选择要添加的 Tags</strong></div>
                <div className="repository-tag-choice-list">
                  {availableTags.length ? availableTags.map((tag) => (
                    <Form.Check key={`add-${tag}`} type="checkbox" id={`add-${tag}`} checked={addTags.has(tag)} onChange={() => toggleBulk(tag, 'add')} label={tag} />
                  )) : <span className="repository-no-tags">还没有可复用的 Tag，可在下方直接创建。</span>}
                </div>
              </section>
              <section>
                <div className="repository-tag-section-title"><span className="remove">删除</span><strong>选择要删除的 Tags</strong></div>
                <div className="repository-tag-choice-list">
                  {availableTags.length ? availableTags.map((tag) => (
                    <Form.Check key={`remove-${tag}`} type="checkbox" id={`remove-${tag}`} checked={removeTags.has(tag)} onChange={() => toggleBulk(tag, 'remove')} label={tag} />
                  )) : <span className="repository-no-tags">当前没有可删除的 Tag。</span>}
                </div>
              </section>
            </div>
          ) : (
            <section className="repository-single-tag-section">
              <div className="repository-tag-section-title"><span className="keep">保留</span><strong>勾选保存后仍保留的 Tags</strong></div>
              <div className="repository-tag-choice-list">
                {allSingleTags.length ? allSingleTags.map((tag) => (
                  <Form.Check key={tag} type="checkbox" id={`single-${tag}`} checked={selectedTags.has(tag)} onChange={() => toggleSingle(tag)} label={tag} />
                )) : <span className="repository-no-tags">这个仓库还没有 Tag，可在下方创建。</span>}
              </div>
            </section>
          )}

          <section className="repository-custom-tag-section">
            <label htmlFor="repository-custom-tags"><strong>创建并添加自定义 Tags</strong><span>支持回车、逗号或换行，一次录入多个。</span></label>
            <Form.Control id="repository-custom-tags" as="textarea" rows={3} value={customTags} onChange={(event) => setCustomTags(event.target.value)} placeholder="例如：核心仓库, backend, 待迁移" />
            {custom.length > 0 && <div className="repository-tag-preview">{custom.map((tag) => <span key={tag}>{tag}</span>)}</div>}
          </section>

          <div className="repository-tag-change-summary">
            <span><i className="add" />将新增 <strong>{bulk ? bulkAdditions.length : singleAdditions.length}</strong> 个 Tag</span>
            <span><i className="remove" />将删除 <strong>{bulk ? removeTags.size : singleRemovals.length}</strong> 个 Tag</span>
            {bulk && <span>影响 <strong>{repositories.length}</strong> 个仓库</span>}
          </div>
          {error && <div className="inline-error"><CircleAlert size={16} />{error}</div>}
        </Modal.Body>
        <Modal.Footer>
          <Button variant="outline-secondary" onClick={onClose} disabled={submitting}>取消</Button>
          <Button type="submit" variant="primary" disabled={!canSave || submitting}>{submitting ? <RefreshCw size={16} className="spin" /> : <Check size={16} />}{submitting ? '正在保存' : '保存 Tags'}</Button>
        </Modal.Footer>
      </form>
    </Modal>
  )
}

interface RepositoryPageProps {
  onOpenSettings: () => void
}

export default function RepositoryPage({ onOpenSettings }: RepositoryPageProps) {
  const [overview, setOverview] = useState(EMPTY_OVERVIEW)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [provider, setProvider] = useState('all')
  const [tag, setTag] = useState('all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [tagModal, setTagModal] = useState<{ repositories: RepositoryOverviewItem[]; bulk: boolean } | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [notice, setNotice] = useState<{ message: string; tone: 'success' | 'error' } | null>(null)

  const loadRepositories = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true)
    else setLoading(true)
    try {
      const response = await api.listRepositoryOverview({
        offset: (page - 1) * pageSize,
        limit: pageSize,
        provider: provider === 'all' ? undefined : provider,
        query: debouncedSearch || undefined,
        tags: tag === 'all' ? [] : [tag],
      })
      setOverview(response)
      setError('')
      const visibleIds = new Set(response.items.map((repository) => repository.id))
      setSelectedIds((current) => new Set([...current].filter((id) => visibleIds.has(id))))
      const lastPage = Math.max(1, Math.ceil(response.total / pageSize))
      if (page > lastPage) setPage(lastPage)
    } catch (requestError) {
      setError(messageFrom(requestError))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [debouncedSearch, page, pageSize, provider, tag])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSearch(search.trim())
      setPage(1)
    }, 300)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    loadRepositories()
    const timer = window.setInterval(() => {
      if (!document.hidden && !tagModal) loadRepositories(true)
    }, 15000)
    return () => window.clearInterval(timer)
  }, [loadRepositories, tagModal])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), notice.tone === 'error' ? 5000 : 3500)
    return () => window.clearTimeout(timer)
  }, [notice])

  async function syncRepositories() {
    setSyncing(true)
    setNotice(null)
    try {
      const response = await api.syncRepositories()
      setNotice({
        message: response.total
          ? `已同步 ${response.total} 个 Webhook 仓库`
          : '仓库目录已是最新',
        tone: 'success',
      })
      await loadRepositories(true)
    } catch (requestError) {
      setNotice({ message: `同步失败：${messageFrom(requestError)}`, tone: 'error' })
    } finally {
      setSyncing(false)
    }
  }

  function toggleSelected(id: string) {
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function togglePageSelection() {
    const allSelected = overview.items.length > 0 && overview.items.every((repository) => selectedIds.has(repository.id))
    setSelectedIds(allSelected ? new Set() : new Set(overview.items.map((repository) => repository.id)))
  }

  const selectedRepositories = overview.items.filter((repository) => selectedIds.has(repository.id))
  const summary = overview.summary

  return (
    <>
      <section className="page-heading repository-page-heading">
        <div>
          <p className="eyebrow">REPOSITORY REGISTRY / 质量目录</p>
          <h1>仓库管理</h1>
          <p>维护仓库标签，并观察检视意见是否真正得到处理。</p>
        </div>
        <div className="repository-page-actions">
          {selectedRepositories.length > 0 && <Button variant="outline-primary" onClick={() => setTagModal({ repositories: selectedRepositories, bulk: selectedRepositories.length > 1 })}><Tags size={16} />{selectedRepositories.length > 1 ? '批量管理 Tags' : '管理 Tags'} <span>{selectedRepositories.length}</span></Button>}
          <Button variant="primary" onClick={syncRepositories} disabled={syncing} aria-busy={syncing}>{syncing ? <RefreshCw size={16} className="spin" /> : <Webhook size={16} />}{syncing ? '正在同步' : '同步 Webhook 仓库'}</Button>
          <button className="icon-button" onClick={() => loadRepositories(true)} aria-label="刷新仓库"><RefreshCw size={17} className={refreshing ? 'spin' : ''} /></button>
        </div>
      </section>

      <section className="repository-ledger" aria-label="仓库质量汇总">
        <div className="repository-ledger-thesis"><span>当前目录</span><strong>{summary.repository_total}</strong><small>个仓库纳入质量观察</small></div>
        <div><span>执行检视</span><strong>{summary.review_total}</strong><small>全部检查批次</small></div>
        <div><span>发现问题</span><strong>{summary.issue_total}</strong><small>结构化检视意见</small></div>
        <div className="accepted"><span>确认处理</span><strong>{summary.accepted_issues}</strong><small>合入后确认修复</small></div>
        <div className="unhandled"><span>合入未处理</span><strong>{summary.unhandled_issues}</strong><small>{summary.pending_issues} 个仍待确认</small></div>
      </section>

      <section className="repository-panel">
        <div className="repository-toolbar">
          <div><p className="eyebrow">CATALOG INDEX</p><h2>仓库登记册</h2><span>找到 {overview.total.toLocaleString('zh-CN')} 个仓库</span></div>
          <div className="repository-filter-tools">
            <label className="repository-search"><Search size={16} /><Form.Control value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索平台或仓库" aria-label="搜索仓库" />{search && <button onClick={() => setSearch('')} aria-label="清除搜索"><X size={13} /></button>}</label>
            <Form.Select value={provider} onChange={(event) => { setProvider(event.target.value); setPage(1) }} aria-label="按平台筛选"><option value="all">全部平台</option>{summary.providers.map((item) => <option key={item} value={item}>{item}</option>)}</Form.Select>
            <Form.Select value={tag} onChange={(event) => { setTag(event.target.value); setPage(1) }} aria-label="按 Tag 筛选"><option value="all">全部 Tags</option>{summary.tags.map((item) => <option key={item} value={item}>{item}</option>)}</Form.Select>
          </div>
        </div>

        <div className="repository-list-wrap">
          {loading ? (
            <div className="state-message"><RefreshCw size={24} className="spin" /><strong>正在读取仓库目录</strong><p>关联检视批次和问题结论…</p></div>
          ) : error ? (
            <div className="state-message error-state"><CircleAlert size={26} /><strong>无法读取仓库</strong><p>{error}</p><div>{error === 'invalid api token' && <Button variant="outline-secondary" onClick={onOpenSettings}><KeyRound size={16} />填写 Token</Button>}<Button variant="primary" onClick={() => loadRepositories()}><RefreshCw size={16} />重试连接</Button></div></div>
          ) : overview.items.length === 0 ? (
            <div className="state-message"><LibraryBig size={27} /><strong>{summary.repository_total ? '没有匹配的仓库' : '仓库目录还是空的'}</strong><p>{summary.repository_total ? '调整平台、Tag 或搜索条件后再试。' : '通过仓库 REST API 添加第一条仓库记录。'}</p></div>
          ) : (
            <>
              <Table hover className="repository-table">
                <colgroup>
                  <col className="repository-select-column" />
                  <col className="repository-identity-column" />
                  <col className="repository-tags-column" />
                  <col className="repository-review-column" />
                  <col className="repository-issues-column" />
                  <col className="repository-updated-column" />
                </colgroup>
                <thead><tr><th scope="col" className="repository-select-cell"><Form.Check type="checkbox" checked={overview.items.every((repository) => selectedIds.has(repository.id))} onChange={togglePageSelection} aria-label="选择当前页全部仓库" /></th><th scope="col">仓库</th><th scope="col">Tags</th><th scope="col">检视</th><th scope="col">问题结论</th><th scope="col">更新时间</th></tr></thead>
                <tbody>{overview.items.map((repository) => (
                  <tr key={repository.id} className={selectedIds.has(repository.id) ? 'selected' : ''}>
                    <td className="repository-select-cell"><Form.Check type="checkbox" checked={selectedIds.has(repository.id)} onChange={() => toggleSelected(repository.id)} aria-label={`选择 ${repository.project_path}`} /></td>
                    <td><div className="repository-identity"><span>{repository.provider}</span><strong>{repository.project_path}</strong>{repository.web_url ? <a href={repository.web_url} target="_blank" rel="noreferrer">打开仓库<ExternalLink size={12} /></a> : <small>未配置跳转地址</small>}</div></td>
                    <td><div className="repository-tags-readonly">{repository.tags.length ? repository.tags.map((item) => <span key={item}>{item}</span>) : <em>暂无 Tag</em>}</div></td>
                    <td><div className="repository-review-count"><strong>{repository.review_statistics.review_total}</strong><span>次检视</span></div></td>
                    <td><IssueBalance repository={repository} /></td>
                    <td><span className="date-cell">{formatDate(repository.updated_at)}</span></td>
                  </tr>
                ))}</tbody>
              </Table>

              <div className="repository-mobile-list">{overview.items.map((repository) => (
                <article key={repository.id} className={selectedIds.has(repository.id) ? 'selected' : ''}>
                  <header><Form.Check type="checkbox" checked={selectedIds.has(repository.id)} onChange={() => toggleSelected(repository.id)} aria-label={`选择 ${repository.project_path}`} /><div><span>{repository.provider}</span><strong>{repository.project_path}</strong></div>{repository.web_url && <a href={repository.web_url} target="_blank" rel="noreferrer" aria-label={`打开 ${repository.project_path}`}><ExternalLink size={16} /></a>}</header>
                  <div className="repository-tags-readonly">{repository.tags.length ? repository.tags.map((item) => <span key={item}>{item}</span>) : <em>暂无 Tag</em>}</div>
                  <div className="repository-mobile-review"><span><GitBranch size={14} /><b>{repository.review_statistics.review_total}</b> 次检视</span><IssueBalance repository={repository} /></div>
                  <footer><small>{formatDate(repository.updated_at)} 更新</small></footer>
                </article>
              ))}</div>
            </>
          )}
        </div>

        {!loading && !error && overview.items.length > 0 && <div className="panel-footer"><Pagination page={page} pageSize={pageSize} total={overview.total} itemLabel="仓库" onPageChange={setPage} onPageSizeChange={(value) => { setPageSize(value); setPage(1) }} /><span className="panel-source"><Server size={13} />仓库目录与检视数据聚合</span></div>}
      </section>

      {tagModal && <TagManagerModal repositories={tagModal.repositories} availableTags={summary.tags} bulk={tagModal.bulk} onClose={() => setTagModal(null)} onSaved={(message) => { setTagModal(null); setSelectedIds(new Set()); setNotice({ message, tone: 'success' }); loadRepositories(true) }} />}
      {notice && <div className={`toast ${notice.tone === 'error' ? 'error-toast' : ''}`} role={notice.tone === 'error' ? 'alert' : 'status'}>{notice.tone === 'error' ? <CircleAlert size={17} /> : <Check size={17} />}{notice.message}</div>}
    </>
  )
}
