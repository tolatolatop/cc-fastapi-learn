export type TaskStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'abandoned'

export interface TaskItem {
  id: string
  status: TaskStatus
  queue_name: string
  prompt: string
  model: string
  metadata: Record<string, unknown> | null
  priority: number
  attempt: number
  max_attempts: number
  session_id: string | null
  agent_mode: boolean
  unattended: boolean
  created_at: string
  started_at: string | null
  finished_at: string | null
  abandoned_at: string | null
  abandoned_reason: string | null
  error_message: string | null
  result: Record<string, unknown> | null
}

export interface TaskListResponse {
  items: TaskItem[]
  total: number
}

export interface QueueItem {
  name: string
  workers: number
  is_default: boolean
}

export interface QueueListResponse {
  items: QueueItem[]
}

export interface TaskLog {
  id: number
  task_id: string
  ts: string
  level: string
  event_type: string
  message: string
  metadata: Record<string, unknown> | null
}

export interface TaskLogListResponse {
  items: TaskLog[]
  total: number
}

export interface TaskContext {
  task_id: string
  messages: string[]
  updated_at: string | null
}

export interface CreateTaskPayload {
  prompt: string
  model?: string
  queue_name?: string
  priority: number
  agent_mode: boolean
  unattended: boolean
  max_attempts?: number
  metadata?: Record<string, unknown>
}

export interface WebhookTrigger {
  id: number
  provider: string
  event_type: string
  event_uuid: string | null
  webhook_uuid: string | null
  instance_url: string | null
  task_id: string
  payload: Record<string, unknown>
  created_at: string
}

export interface WebhookTriggerListResponse {
  items: WebhookTrigger[]
  total: number
}
