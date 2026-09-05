---
name: platform-review
description: Check out GitLab merge requests and list, create, reply to, resolve, or batch-process live GitLab review discussions through platform_review.py. Use for live provider operations; do not use for cc-fastapi internal task or review records.
---

# Platform review

Use the repository's standalone `platform_review.py` adapter for live platform
operations. It currently supports GitLab merge-request URLs only. For another
provider, stop and implement or select the corresponding `ReviewPlatform`
adapter instead of treating its URL as GitLab.

## Credentials and host scope

Expect the GitLab token in `GITLAB_TOKEN`, or use `--token-env` to name a
different environment variable. Never print the token or pass its value as a
command argument.

Use `PLATFORM_ALLOWED_HOSTS` or repeat `--allowed-host` when the allowed GitLab
instances are known. Retain the full MR URL supplied by the caller; do not
silently substitute a different host, project, or MR number.

Run commands from the repository root as:

```bash
python platform_review.py COMMAND ...
```

Global options such as `--allowed-host` must appear before `COMMAND`.

## Prepare code

```bash
python platform_review.py checkout MR_URL DESTINATION
```

The destination must be absent or empty. The command checks out GitLab's
`refs/merge-requests/:iid/head` at the exact returned `head_sha`, including for
fork-backed MRs. Use the local Git checkout for diffs and code inspection; this
adapter intentionally has no diff API.

Preserve the returned `head_sha`. A later line-comment operation must use that
value and should fail rather than comment on a newer MR version unnoticed.

## Read discussions

```bash
python platform_review.py review-list MR_URL --status open
python platform_review.py review-get MR_URL THREAD_ID
```

`review-list` can filter by `--status`, `--file-path`, `--author`, and
`--updated-after`. Use the returned provider thread ID for replies and resolve
operations.

## Write discussions

Create a line-level discussion with the file path and line coordinates from the
local Git inspection:

```bash
python platform_review.py review-create MR_URL \
  --file-path src/example.py --line 42 --side new \
  --head-sha HEAD_SHA --body-file BODY_FILE \
  --operation-id STABLE_OPERATION_ID
```

- `--side new` addresses an added or resulting line; `--side old` addresses a
  removed line.
- Use a stable, unique `--operation-id` for create and reply operations so an
  Agent retry does not duplicate a comment.
- Prefer `--body-file` for multiline text. `--body-file -` reads stdin.
- Reply with `review-reply MR_URL THREAD_ID`; close a discussion with
  `review-resolve MR_URL THREAD_ID`.
- Inspect the target before a write when the caller has not supplied a verified
  thread ID or head SHA. A read-only request does not authorize a write.

For multiple actions, read [references/batch-csv.md](references/batch-csv.md)
and use the `batch` command.

## Results and boundaries

Commands emit JSON. Exit code 2 indicates an input or platform error. A batch
with one or more failed rows emits all row results and exits with code 3.

This adapter does not update cc-fastapi task, batch, or finding records. Use the
`cc-fastapi-admin` skill when the requested outcome includes internal records.
Do not duplicate existing Webhook ingestion or infer that a live discussion
operation changed internal verification state.
