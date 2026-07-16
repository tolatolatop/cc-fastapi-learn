import type {
  CreateTaskPayload,
  QueueListResponse,
  TaskContext,
  TaskItem,
  TaskListResponse,
  TaskLogListResponse,
} from './types'

const API_ROOT = '/api'

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
  listTasks: () => request<TaskListResponse>('/v1/agent-tasks?limit=200'),
  listQueues: () => request<QueueListResponse>('/v1/agent-tasks/queues/available'),
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
