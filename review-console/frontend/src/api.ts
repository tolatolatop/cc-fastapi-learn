import type { AuthConfig, DecisionReason, Grant, HistoryItem, Issue, IssueStatus, PullRequest, Repository, Statistics, User } from './types'

const root = '/api'
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${root}${path}`, {
    ...init,
    credentials: 'same-origin',
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string | Array<{ msg: string }> }
    const detail = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg).join('；') : payload.detail
    const error = new Error(detail || `请求失败（${response.status}）`) as Error & { status?: number }
    error.status = response.status
    throw error
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  authConfig: () => request<AuthConfig>('/v1/auth/config'),
  login: (username: string, password: string) => request<{ user: User }>('/v1/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  logout: () => request<void>('/v1/auth/logout', { method: 'POST' }),
  me: () => request<User>('/v1/auth/me'),
  repositories: () => request<{ items: Repository[] }>('/v1/repositories'),
  queue: (query: string, offset: number, limit: number, signal?: AbortSignal) => {
    const params = new URLSearchParams({ q: query, offset: String(offset), limit: String(limit) })
    return request<{ items: PullRequest[]; total: number }>(`/v1/queue?${params}`, { signal })
  },
  pullRequest: (repo: Repository, prNumber: string, signal?: AbortSignal) => {
    const params = new URLSearchParams({ provider: repo.provider, project_path: repo.project_path, pr_number: prNumber })
    return request<PullRequest>(`/v1/pull-request?${params}`, { signal })
  },
  issues: (repo: Repository, prNumber: string, status: string, query: string, hideLow: boolean, pendingFirst: boolean, offset: number, limit: number, signal?: AbortSignal) => {
    const params = new URLSearchParams({ provider: repo.provider, project_path: repo.project_path, pr_number: prNumber, offset: String(offset), limit: String(limit), sort: pendingFirst ? 'pending_first' : 'location' })
    if (status) params.append('status', status)
    if (query) params.append('q', query)
    if (hideLow) ['critical', 'high', 'medium', 'info'].forEach((severity) => params.append('severity', severity))
    return request<{ items: Issue[]; total: number }>(`/v1/issues?${params}`, { signal })
  },
  history: (id: string) => request<{ items: HistoryItem[] }>(`/v1/issues/${id}/history`),
  updateStatus: (issue: Issue, status: IssueStatus, reasonCode: DecisionReason | null, note: string | null) => request<Issue>(`/v1/issues/${issue.id}/status`, {
    method: 'PUT', body: JSON.stringify({ status, reason_code: reasonCode, note, expected_updated_at: issue.updated_at }),
  }),
  statistics: (createdFrom: string, createdTo: string) => {
    const params = new URLSearchParams()
    if (createdFrom) params.set('created_from', new Date(createdFrom).toISOString())
    if (createdTo) params.set('created_to', new Date(createdTo).toISOString())
    return request<Statistics>(`/v1/statistics${params.size ? `?${params}` : ''}`)
  },
  users: () => request<{ items: User[] }>('/v1/admin/users'),
  createUser: (payload: { username: string; display_name: string; password: string; is_admin: boolean }) => request<User>('/v1/admin/users', { method: 'POST', body: JSON.stringify(payload) }),
  updateUser: (id: string, payload: Partial<Pick<User, 'display_name' | 'is_admin' | 'is_active'>> & { password?: string }) => request<User>(`/v1/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  putGrant: (userId: string, payload: Omit<Grant, 'id'>) => request<Grant>(`/v1/admin/users/${userId}/grants`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteGrant: (userId: string, grantId: string) => request<void>(`/v1/admin/users/${userId}/grants/${grantId}`, { method: 'DELETE' }),
}
