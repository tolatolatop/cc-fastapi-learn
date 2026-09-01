---
name: cc-fastapi-admin
description: Inspect cc-fastapi Agent Tasks and PR/MR histories, record task-backed or standalone structured review findings, and verify tracked findings through cc-fastapi-admin. Use for internal cc-fastapi task and review state; do not use for live GitHub or GitLab discussions or repository checkout.
---

# cc-fastapi-admin

Use `cc-fastapi-admin` as the only interface for cc-fastapi task and review
management. Do not access the service database directly or reproduce these
operations with ad hoc HTTP requests.

## Connection

Expect `CC_FASTAPI_BASE_URL` and, when required, `CC_FASTAPI_TOKEN` to be set by
the operator. Never print, persist, or place the token in a command argument.

Run this before a workflow that depends on the service:

```bash
cc-fastapi-admin status
```

Stop if the command fails or its JSON result does not report `ok: true`.

## Read operations

- Inspect a known task with `cc-fastapi-admin task show TASK_ID`. Read normal
  Agent output from `result.output_text`.
- Discover webhook-backed PR/MR histories with `cc-fastapi-admin pr recent`.
  This is internal history, not a live provider query.
- Inspect the complete known context with
  `cc-fastapi-admin pr show PROVIDER PROJECT_PATH PR_NUMBER`.
- Prefer an explicit task ID, provider, project path, PR/MR number, batch ID, or
  other stable identifier whenever the caller supplies one.

Read-only requests do not authorize `collect`, `add-issues`, or `verify`.

## Write operations

Before changing findings, inspect the target PR/MR and relevant task or batch.
Read [references/write-inputs.md](references/write-inputs.md) for the exact JSON
shapes and selection rules.

- Use `pr collect` for findings extracted from a successful Agent Task. Pass
  `--task-id` when it is known; do not silently attach findings to another task.
- Use `pr add-issues` only for standalone findings that intentionally have no
  Agent Task or tracked post-merge verification lifecycle.
- Use `pr verify` only for tracked findings. Supply `--batch-id` when more than
  one batch could be targeted.
- Submit structured data with `--input -` or an explicit JSON file. Do not put a
  large JSON document in a shell argument.
- Treat conflicts as a reason to re-read current state. Do not blindly retry a
  different target.

Successful commands emit one JSON object to stdout. Preserve returned task,
batch, and issue identifiers when reporting or performing a subsequent step.

## Boundary with the live provider

This tool does not clone a repository or read and modify live GitHub/GitLab
review discussions. Use the `platform-review` skill for those operations. Do
not assume that an internal finding status automatically changed a provider
discussion, or the reverse.
