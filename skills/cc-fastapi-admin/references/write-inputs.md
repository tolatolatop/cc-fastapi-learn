# Write inputs

Read this reference only when recording or verifying findings.

## Required version identity

`collect` and `add-issues` both require the reviewed code version:

- `--head-sha SHA` — commit SHA of the reviewed version (7-64 hex characters).
- `--head-committed-at ISO8601` — commit time of that SHA. Batches are ordered by
  code version, not collection time, so a missing or guessed value would make an
  older report look newer. Future timestamps are rejected.

Prefer the real commit time, for example `git show -s --format=%cI HEAD_SHA`.
Do not substitute the current time.

## Task-backed collection

```bash
cc-fastapi-admin pr collect PROVIDER PROJECT_PATH PR_NUMBER \
  --task-id TASK_ID --head-sha SHA --head-committed-at ISO8601 --input -
```

Input:

```json
{
  "issues": [
    {
      "severity": "high",
      "category": "correctness",
      "title": "Short finding title",
      "description": "Evidence and impact",
      "file_path": "src/example.py",
      "line_number": 42
    }
  ]
}
```

`severity` is `critical`, `high`, `medium`, `low`, or `info`. `category`,
`file_path`, and `line_number` are optional. An issues list can contain at most
500 items. The selected Agent Task and its Workflow must have succeeded.

Without `--task-id`, the command selects the latest active successful task.
Avoid implicit selection when a task ID is available.

## Standalone findings

```bash
cc-fastapi-admin pr add-issues PROVIDER PROJECT_PATH PR_NUMBER \
  --head-sha SHA --head-committed-at ISO8601 --input -
```

The input has the same `{"issues": [...]}` shape and requires between 1 and 500
issues. Standalone findings are intentionally not part of tracked post-merge
verification; do not use this as a fallback when task-backed collection fails.

## Verification

First run `pr show` and identify the intended open batch and its `issue_no`
values. Then run:

```bash
cc-fastapi-admin pr verify PROVIDER PROJECT_PATH PR_NUMBER \
  --batch-id BATCH_ID --input -
```

Input:

```json
{
  "results": [
    {
      "issue_no": 1,
      "status": "accepted",
      "note": "Verification evidence"
    },
    {
      "issue_no": 2,
      "status": "not_accepted",
      "note": "Problem remains"
    }
  ]
}
```

Only `accepted` and `not_accepted` are valid conclusions. Issue numbers must be
unique in one request. Use `--merged-sha SHA` only when Webhook history does not
provide the merged SHA and the caller or inspected repository state provides a
reliable value.
