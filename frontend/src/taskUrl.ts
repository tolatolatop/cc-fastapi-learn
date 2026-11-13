/**
 * Deep-link helpers for the task scheduling board.
 *
 * The console keeps its view state in the hash (`#/tasks`, `#/reviews`, ...), so the
 * selected task travels in the regular query string as `?task=<task id>`. Legacy or
 * hand-written links that put the parameter inside the hash (`#/tasks?task=<id>`) are
 * still understood when reading, and the parameter is normalised to `?task=` on write.
 */

export const TASK_PARAM = 'task'

const TASK_PARAM_KEYS = [TASK_PARAM, 'task_id', 'taskId']

/** Trim the value and drop the `TASK-` prefix the table renders in front of short ids. */
export function normalizeTaskId(raw: string | null | undefined): string | null {
  if (!raw) return null
  const value = raw.trim().replace(/^task[-_:]/i, '').trim()
  return value.length > 0 ? value : null
}

function splitHash(hash: string): { path: string; query: string } {
  const index = hash.indexOf('?')
  if (index === -1) return { path: hash, query: '' }
  return { path: hash.slice(0, index), query: hash.slice(index + 1) }
}

function taskIdFromParams(params: URLSearchParams): string | null {
  for (const key of TASK_PARAM_KEYS) {
    const id = normalizeTaskId(params.get(key))
    if (id) return id
  }
  return null
}

/** Read the task id from `?task=<id>` (or the hash-embedded equivalent). */
export function readTaskId(location: { search: string; hash: string }): string | null {
  return taskIdFromParams(new URLSearchParams(location.search))
    ?? taskIdFromParams(new URLSearchParams(splitHash(location.hash).query))
}

/**
 * Build the URL for the given task id. Passing `null` removes the parameter so the
 * drawer state and the address bar stay in sync.
 */
export function taskUrlFor(id: string | null | undefined, href: string): string {
  const url = new URL(href)
  for (const key of TASK_PARAM_KEYS) url.searchParams.delete(key)

  const normalized = normalizeTaskId(id)
  if (normalized) url.searchParams.set(TASK_PARAM, normalized)

  const { path, query } = splitHash(url.hash)
  if (query) {
    const hashParams = new URLSearchParams(query)
    for (const key of TASK_PARAM_KEYS) hashParams.delete(key)
    const remaining = hashParams.toString()
    url.hash = remaining ? `${path}?${remaining}` : path
  }

  return `${url.pathname}${url.search}${url.hash}`
}

/** Current URL in the same shape `taskUrlFor` produces, for cheap equality checks. */
export function currentUrl(location: { pathname: string; search: string; hash: string }): string {
  return `${location.pathname}${location.search}${location.hash}`
}

/** Push or replace the browser entry so `?task=` mirrors the open drawer. */
export function syncTaskUrl(id: string | null | undefined, mode: 'push' | 'replace' = 'push'): void {
  if (typeof window === 'undefined') return
  const next = taskUrlFor(id, window.location.href)
  if (next === currentUrl(window.location)) return
  window.history[mode === 'push' ? 'pushState' : 'replaceState'](null, '', next)
}
