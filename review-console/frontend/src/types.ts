export type Permission = 'read' | 'write'
export type IssueStatus = 'unverified' | 'accepted' | 'not_accepted' | 'needs_info'
export type DecisionReason = 'false_positive' | 'protected_by_control' | 'not_reproducible' | 'duplicate' | 'out_of_scope' | 'intentional_behavior' | 'risk_accepted' | 'other'
export type VerificationStatus = 'unverified' | 'accepted' | 'not_accepted'
export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info'

export interface Grant { id: string; provider: string; project_path: string; permission: Permission }
export interface User {
  id: string
  username: string
  display_name: string
  is_admin: boolean
  is_active: boolean
  auth_source: 'local' | 'sso'
  created_at: string
  grants: Grant[]
}
export interface AuthConfig {
  local_login_enabled: boolean
  sso_enabled: boolean
  sso_button_label: string
}
export interface Repository {
  provider: string
  project_path: string
  issue_total: number
  pending_total: number
  permission: Permission
}
export type PullRequestCompletionStatus = 'processing' | 'pending' | 'completed' | 'no_issues' | 'failed'
export interface PullRequest {
  provider: string
  project_path: string
  pr_number: string
  pr_url: string | null
  completion_status: PullRequestCompletionStatus
  batch_total: number
  issue_total: number
  reviewed_total: number
  pending_total: number
  updated_at: string
}
export interface Issue {
  id: string
  batch_id: string
  provider: string
  project_path: string
  pr_number: string
  pr_url: string | null
  issue_no: number
  severity: Severity
  category: string | null
  title: string
  description: string
  file_path: string | null
  line_number: number | null
  status: IssueStatus
  reason_code: DecisionReason | null
  note: string | null
  decided_by_id: string | null
  decided_by_name: string | null
  decided_at: string | null
  verification_status: VerificationStatus
  review_head_sha: string | null
  merged_sha: string | null
  batch_status: 'collecting' | 'waiting_merge' | 'verifying' | 'completed' | 'failed' | 'cancelled'
  batch_created_at: string
  batch_extracted_at: string | null
  batch_verified_at: string | null
  created_at: string
  updated_at: string
}
export interface HistoryItem {
  id: string
  previous_status: IssueStatus
  new_status: IssueStatus
  previous_note: string | null
  new_note: string | null
  previous_reason_code: DecisionReason | null
  new_reason_code: DecisionReason | null
  actor_name: string
  dimension: 'decision' | 'verification'
  created_at: string
}
export interface Statistics {
  created_from: string | null
  created_to: string | null
  summary: { valid_opinion_total: number; confirmed_total: number; false_positive_total: number }
  contributors: Array<{ actor_id: string; actor_name: string; confirmed_total: number; accepted_total: number; not_accepted_total: number }>
  top_false_positive_repositories: Array<{ provider: string; project_path: string; false_positive_total: number; rejected_total: number; decided_total: number; false_positive_rate: number }>
}
