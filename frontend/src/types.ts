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
  summary: {
    total: number
    status_counts: Record<TaskStatus, number>
    queues: Array<{
      name: string
      total: number
      queued: number
      running: number
    }>
  }
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
  task_id: string | null
  task_status: TaskStatus | null
  payload: Record<string, unknown>
  created_at: string
  workflow_run_id: string | null
  workflow_status: 'planning' | 'running' | 'skipped' | 'succeeded' | 'failed' | 'superseded' | null
  skip_reason: string | null
}

export interface WebhookTriggerListResponse {
  items: WebhookTrigger[]
  total: number
  summary: {
    total: number
    event_types: string[]
  }
}

export type ReviewBatchStatus = 'collecting' | 'waiting_merge' | 'verifying' | 'completed' | 'failed' | 'cancelled'
export type ReviewIssueSeverity = 'critical' | 'high' | 'medium' | 'low' | 'info'
export type ReviewIssueVerificationStatus = 'unverified' | 'accepted' | 'not_accepted'

export interface ReviewIssueBatch {
  id: string
  provider: string
  instance_url: string | null
  project_path: string
  pr_number: string
  pr_url: string | null
  review_workflow_run_id: string | null
  review_task_id: string
  extract_task_id: string | null
  verify_task_id: string | null
  review_head_sha: string | null
  merged_sha: string | null
  status: ReviewBatchStatus
  issue_count: number
  error_message: string | null
  created_at: string
  extracted_at: string | null
  verified_at: string | null
  updated_at: string
}

export interface ReviewIssueBatchListResponse {
  items: ReviewIssueBatch[]
  total: number
}

export interface ReviewIssue {
  id: string
  batch_id: string
  issue_no: number
  severity: ReviewIssueSeverity
  category: string | null
  title: string
  description: string
  file_path: string | null
  line_number: number | null
  verification_status: ReviewIssueVerificationStatus
  verification_note: string | null
  created_at: string
  verified_at: string | null
  updated_at: string
}

export interface ReviewIssueListResponse {
  items: ReviewIssue[]
  total: number
}

export interface ReviewIssueStatistics {
  batch_total: number
  zero_issue_batches: number
  batch_status_counts: Record<ReviewBatchStatus, number>
  issue_total: number
  verified_issues: number
  accepted_issues: number
  acceptance_rate: number | null
  verification_status_counts: Record<ReviewIssueVerificationStatus, number>
  severity_counts: Record<ReviewIssueSeverity, number>
}

export interface CreateReviewIssueBatchPayload {
  provider: string
  instance_url?: string
  project_path: string
  pr_number: string
  pr_url?: string
  review_workflow_run_id?: string
  review_task_id: string
  extract_task_id?: string
  verify_task_id?: string
  review_head_sha?: string
}

export interface UpdateReviewIssueBatchPayload {
  status?: ReviewBatchStatus
  extract_task_id?: string | null
  verify_task_id?: string | null
  merged_sha?: string | null
  error_message?: string | null
}

export interface CreateReviewIssuePayload {
  severity: ReviewIssueSeverity
  category?: string
  title: string
  description: string
  file_path?: string
  line_number?: number
}

export type ReviewDashboardOutcome = 'all' | 'accepted' | 'unhandled' | 'pending'

export interface ReviewDashboardSummary {
  pull_request_total: number
  batch_total: number
  issue_total: number
  accepted_issues: number
  merged_unhandled_issues: number
  pending_issues: number
  acceptance_rate: number | null
}

export interface ReviewDashboardTrendPoint {
  date: string
  issue_total: number
  accepted_issues: number
  merged_unhandled_issues: number
  pending_issues: number
}

export interface ReviewDashboardRepository {
  provider: string
  project_path: string
  pull_request_total: number
  issue_total: number
}

export interface ReviewDashboardPullRequest {
  provider: string
  project_path: string
  pr_number: string
  pr_url: string | null
  latest_batch_id: string
  latest_batch_status: ReviewBatchStatus
  batch_total: number
  issue_total: number
  accepted_issues: number
  merged_unhandled_issues: number
  pending_issues: number
  latest_activity_at: string
  task_total: number
  task_status_counts: Record<TaskStatus, number>
}

export interface ReviewDashboardTask {
  id: string
  batch_id: string
  role: 'review' | 'extract' | 'verify'
  status: TaskStatus
  session_id: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error_message: string | null
}

export interface ReviewDashboardResponse {
  summary: ReviewDashboardSummary
  timeline: ReviewDashboardTrendPoint[]
  repositories: ReviewDashboardRepository[]
  tags: string[]
  items: ReviewDashboardPullRequest[]
  total: number
}

export interface ReviewDashboardPullRequestDetail {
  pull_request: ReviewDashboardPullRequest
  batches: ReviewIssueBatch[]
  tasks: ReviewDashboardTask[]
}

export interface RepositoryItem {
  id: string
  provider: string
  project_path: string
  web_url: string | null
  tags: string[]
  created_at: string
  updated_at: string
}

export interface RepositoryReviewStatistics {
  review_total: number
  issue_total: number
  accepted_issues: number
  unhandled_issues: number
  pending_issues: number
}

export interface RepositoryOverviewItem extends RepositoryItem {
  review_statistics: RepositoryReviewStatistics
}

export interface RepositoryOverviewResponse {
  items: RepositoryOverviewItem[]
  total: number
  summary: RepositoryReviewStatistics & {
    repository_total: number
    providers: string[]
    tags: string[]
  }
}

export interface RepositoryBulkTagsUpdateResponse {
  items: RepositoryItem[]
  total: number
}
