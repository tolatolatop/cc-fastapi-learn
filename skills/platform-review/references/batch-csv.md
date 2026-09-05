# Batch CSV

Read this reference when applying multiple live GitLab discussion operations.

Use this exact UTF-8 CSV header:

```csv
operation_id,action,thread_id,file_path,line,side,body,head_sha
```

Supported actions and required fields:

| Action | Required fields |
| --- | --- |
| `create` | `operation_id`, `file_path`, `line`, `side`, `body`, `head_sha` |
| `reply` | `operation_id`, `thread_id`, `body` |
| `resolve` | `operation_id`, `thread_id` |

Example:

```csv
operation_id,action,thread_id,file_path,line,side,body,head_sha
review-42-001,create,,src/example.py,42,new,"Possible null dereference",abc123
review-42-002,reply,discussion-88,,,,"The issue remains in the latest push",
review-42-003,resolve,discussion-99,,,,,
```

Quote multiline or comma-containing bodies according to standard CSV rules.
Apply the file with:

```bash
python platform_review.py batch MR_URL OPERATIONS.csv
```

Use `-` instead of a file path to read CSV from stdin. Inspect every row result;
the adapter continues after a row failure. `succeeded`, `skipped`, and `failed`
are separate outcomes. A skipped create or reply normally means its
`operation_id` was already applied; a skipped resolve normally means the thread
was already resolved.
