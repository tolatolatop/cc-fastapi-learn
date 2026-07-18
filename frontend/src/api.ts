import type {
  CreateReviewIssueBatchPayload,
  CreateReviewIssuePayload,
  CreateTaskPayload,
  QueueListResponse,
  RepositoryBulkTagsUpdateResponse,
  RepositoryItem,
  RepositoryOverviewResponse,
  ReviewDashboardOutcome,
  ReviewDashboardPullRequestDetail,
  ReviewDashboardResponse,
  ReviewBatchStatus,
  ReviewIssue,
  ReviewIssueBatch,
  ReviewIssueBatchListResponse,
  ReviewIssueListResponse,
  ReviewIssueSeverity,
  ReviewIssueStatistics,
  ReviewIssueVerificationStatus,
  TaskContext,
  TaskItem,
  TaskListResponse,
  TaskLogListResponse,
  UpdateReviewIssueBatchPayload,
  WebhookTriggerListResponse,
} from './types'

const API_ROOT = '/api'

interface TaskListOptions {
  offset?: number
  limit?: number
  statuses?: string[]
  queue?: string
  query?: string
}

interface WebhookListOptions {
  offset?: number
  limit?: number
  eventType?: string
  provider?: string
  query?: string
}

interface ReviewBatchListOptions {
  offset?: number
  limit?: number
  provider?: string
  projectPath?: string
  prNumber?: string
  statuses?: ReviewBatchStatus[]
}

interface ReviewIssueListOptions {
  offset?: number
  limit?: number
  batchId?: string
  provider?: string
  projectPath?: string
  prNumber?: string
  severities?: ReviewIssueSeverity[]
  statuses?: ReviewIssueVerificationStatus[]
  category?: string
  createdFrom?: string
  createdTo?: string
  batchCreatedFrom?: string
  batchCreatedTo?: string
}

interface ReviewStatisticsOptions {
  provider?: string
  projectPath?: string
  prNumber?: string
}

interface ReviewDashboardOptions {
  offset?: number
  limit?: number
  provider?: string
  projectPath?: string
  tag?: string
  createdFrom?: string
  createdTo?: string
  outcome?: ReviewDashboardOutcome
}

interface RepositoryOverviewOptions {
  offset?: number
  limit?: number
  provider?: string
  query?: string
  tags?: string[]
}

function queryPath(path: string, values: Array<[string, string | number | undefined]>) {
  const params = new URLSearchParams()
  values.forEach(([key, value]) => {
    if (value !== undefined && value !== '') params.append(key, String(value))
  })
  const query = params.toString()
  return query ? `${path}?${query}` : path
}

function tokenHeaders(): HeadersInit {
  const token = localStorage.getItem('cc-api-token')?.trim()
  return token ? { 'X-API-Token': token } : {}
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      ...tokenHeaders(),
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload.detail) message = payload.detail
    } catch {
      // Keep the HTTP fallback when the upstream did not return JSON.
    }
    const error = new Error(message) as Error & { status?: number }
    error.status = response.status
    throw error
  }

  return response.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string }>('/healthz'),
  listTasks: ({ offset = 0, limit = 20, statuses = [], queue, query }: TaskListOptions = {}) => request<TaskListResponse>(queryPath('/v1/agent-tasks', [
    ['offset', offset],
    ['limit', limit],
    ...statuses.map((status): [string, string] => ['status', status]),
    ['queue', queue],
    ['q', query],
  ])),
  listQueues: () => request<QueueListResponse>('/v1/agent-tasks/queues/available'),
  listWebhooks: ({ offset = 0, limit = 20, eventType, provider, query }: WebhookListOptions = {}) => request<WebhookTriggerListResponse>(queryPath('/v1/webhooks', [
    ['offset', offset],
    ['limit', limit],
    ['event_type', eventType],
    ['provider', provider],
    ['q', query],
  ])),
  getTask: (id: string) => request<TaskItem>(`/v1/agent-tasks/${id}`),
  getLogs: (id: string) => request<TaskLogListResponse>(`/v1/agent-tasks/${id}/logs?limit=500`),
  getContext: (id: string) => request<TaskContext>(`/v1/agent-tasks/${id}/context`),
  createTask: (payload: CreateTaskPayload) =>
    request<{ task_id: string; status: string; queue_name: string }>('/v1/agent-tasks', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  cancelTask: (id: string) =>
    request<{ task_id: string; status: string }>(`/v1/agent-tasks/${id}/cancel`, { method: 'POST' }),
  retryTask: (id: string) =>
    request<{ task_id: string; status: string; queue_name: string }>(`/v1/agent-tasks/${id}/retry`, { method: 'POST' }),
  listReviewBatches: ({
    offset = 0,
    limit = 20,
    provider,
    projectPath,
    prNumber,
    statuses = [],
  }: ReviewBatchListOptions = {}) => request<ReviewIssueBatchListResponse>(queryPath('/v1/review-issue-batches', [
    ['offset', offset],
    ['limit', limit],
    ['provider', provider],
    ['project_path', projectPath],
    ['pr_number', prNumber],
    ...statuses.map((status): [string, string] => ['status', status]),
  ])),
  getReviewBatch: (id: string) => request<ReviewIssueBatch>(`/v1/review-issue-batches/${id}`),
  createReviewBatch: (payload: CreateReviewIssueBatchPayload) =>
    request<ReviewIssueBatch>('/v1/review-issue-batches', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateReviewBatch: (id: string, payload: UpdateReviewIssueBatchPayload) =>
    request<ReviewIssueBatch>(`/v1/review-issue-batches/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  createReviewIssues: (batchId: string, items: CreateReviewIssuePayload[]) =>
    request<ReviewIssueListResponse>(`/v1/review-issue-batches/${batchId}/issues`, {
      method: 'POST',
      body: JSON.stringify({ items }),
    }),
  listReviewIssues: ({
    offset = 0,
    limit = 20,
    batchId,
    provider,
    projectPath,
    prNumber,
    severities = [],
    statuses = [],
    category,
    createdFrom,
    createdTo,
    batchCreatedFrom,
    batchCreatedTo,
  }: ReviewIssueListOptions = {}) => request<ReviewIssueListResponse>(queryPath('/v1/review-issues', [
    ['offset', offset],
    ['limit', limit],
    ['batch_id', batchId],
    ['provider', provider],
    ['project_path', projectPath],
    ['pr_number', prNumber],
    ...severities.map((severity): [string, string] => ['severity', severity]),
    ...statuses.map((status): [string, string] => ['status', status]),
    ['category', category],
    ['created_from', createdFrom],
    ['created_to', createdTo],
    ['batch_created_from', batchCreatedFrom],
    ['batch_created_to', batchCreatedTo],
  ])),
  updateReviewIssue: (
    id: string,
    payload: { status: Exclude<ReviewIssueVerificationStatus, 'unverified'>; note?: string | null },
  ) => request<ReviewIssue>(`/v1/review-issues/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  }),
  getReviewStatistics: ({ provider, projectPath, prNumber }: ReviewStatisticsOptions = {}) =>
    request<ReviewIssueStatistics>(queryPath('/v1/review-issues/summary', [
      ['provider', provider],
      ['project_path', projectPath],
      ['pr_number', prNumber],
    ])),
  getReviewDashboard: ({
    offset = 0,
    limit = 20,
    provider,
    projectPath,
    tag,
    createdFrom,
    createdTo,
    outcome = 'all',
  }: ReviewDashboardOptions = {}) => request<ReviewDashboardResponse>(queryPath('/v1/review-dashboard', [
    ['offset', offset],
    ['limit', limit],
    ['provider', provider],
    ['project_path', projectPath],
    ['tag', tag],
    ['created_from', createdFrom],
    ['created_to', createdTo],
    ['outcome', outcome],
  ])),
  getReviewDashboardPullRequest: (provider: string, projectPath: string, prNumber: string) =>
    request<ReviewDashboardPullRequestDetail>(queryPath('/v1/review-dashboard/pull-request', [
      ['provider', provider],
      ['project_path', projectPath],
      ['pr_number', prNumber],
    ])),
  listRepositoryOverview: ({
    offset = 0,
    limit = 20,
    provider,
    query,
    tags = [],
  }: RepositoryOverviewOptions = {}) => request<RepositoryOverviewResponse>(queryPath('/v1/repositories/overview', [
    ['offset', offset],
    ['limit', limit],
    ['provider', provider],
    ['q', query],
    ...tags.map((tag): [string, string] => ['tag', tag]),
  ])),
  replaceRepositoryTags: (repositoryId: string, tags: string[]) =>
    request<RepositoryItem>(`/v1/repositories/${repositoryId}/tags`, {
      method: 'PUT',
      body: JSON.stringify({ tags }),
    }),
  bulkUpdateRepositoryTags: (payload: { repository_ids: string[]; add_tags: string[]; remove_tags: string[] }) =>
    request<RepositoryBulkTagsUpdateResponse>('/v1/repositories/tags', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
}
