import type {
  CreateTaskPayload,
  QueueListResponse,
  TaskContext,
  TaskItem,
  TaskListResponse,
  TaskLogListResponse,
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
  query?: string
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
  listWebhooks: ({ offset = 0, limit = 20, eventType, query }: WebhookListOptions = {}) => request<WebhookTriggerListResponse>(queryPath('/v1/webhooks', [
    ['offset', offset],
    ['limit', limit],
    ['event_type', eventType],
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
}
