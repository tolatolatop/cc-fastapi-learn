from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from cc_fastapi.admin_client import (
    AdminApiClient,
    AdminClientError,
    PullRequestIdentity,
    parse_add_issues_input,
    parse_collect_input,
    parse_verify_input,
    read_json_input,
)


class AdminArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)


def _identity(args: argparse.Namespace) -> PullRequestIdentity:
    return PullRequestIdentity(args.provider, args.project_path, args.pr_number)


def build_parser() -> argparse.ArgumentParser:
    parser = AdminArgumentParser(
        prog="cc-fastapi-admin",
        description=(
            "Inspect Agent Tasks and manage PR/MR review findings through the "
            "cc-fastapi API.\n"
            "All successful commands write one JSON object to stdout."
        ),
        epilog=(
            "Examples:\n"
            "  cc-fastapi-admin status\n"
            "  cc-fastapi-admin task show TASK_ID\n"
            "  cc-fastapi-admin pr recent --limit 10\n"
            "  cc-fastapi-admin pr show github org/project 42\n\n"
            "Connection options must appear before the command. Prefer the "
            "CC_FASTAPI_BASE_URL and CC_FASTAPI_TOKEN environment variables."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("CC_FASTAPI_BASE_URL", ""),
        help="API base URL (default: CC_FASTAPI_BASE_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("CC_FASTAPI_TOKEN", ""),
        help=argparse.SUPPRESS,
    )
    commands = parser.add_subparsers(
        dest="command", required=True, title="commands", metavar="COMMAND"
    )
    commands.add_parser(
        "status",
        help="check API connectivity and report the resolved service URL",
        description=(
            "Call /healthz and print the resolved API base URL. This verifies "
            "connectivity without exposing the API token."
        ),
    )
    task = commands.add_parser(
        "task",
        help="inspect an Agent Task directly by task ID",
        description=(
            "Inspect an Agent Task without first resolving a provider, repository, "
            "or PR/MR."
        ),
    )
    task_commands = task.add_subparsers(
        dest="task_command", required=True, title="task commands", metavar="COMMAND"
    )
    task_show = task_commands.add_parser(
        "show",
        help="show the task prompt, status, and execution result",
        description=(
            "Fetch one Agent Task by ID. The original request is in `prompt`, the "
            "complete execution result is in `result`, and normal text output is "
            "in `result.output_text`."
        ),
        epilog="Example:\n  cc-fastapi-admin task show 9f997a61-...",
    )
    task_show.add_argument("task_id", metavar="TASK_ID", help="Agent Task ID")
    pr = commands.add_parser(
        "pr",
        help="inspect PR/MR histories and manage structured review findings",
        description=(
            "PR-centric commands for finding review Tasks, reading results, "
            "recording findings, and verifying tracked outcomes."
        ),
    )
    pr_commands = pr.add_subparsers(
        dest="pr_command", required=True, title="PR commands", metavar="COMMAND"
    )

    recent = pr_commands.add_parser(
        "recent",
        help="list PRs/MRs recently observed through Webhooks",
        description=(
            "List PRs/MRs known from Webhook history with their latest Workflow "
            "and Task status. This is not a live query to GitHub or GitLab, and "
            "standalone-only issue records are not included."
        ),
        epilog="Example:\n  cc-fastapi-admin pr recent --provider github --state open",
    )
    recent.add_argument("--provider", help="filter by provider, for example github")
    recent.add_argument(
        "--project-path", help="filter by repository path, for example org/project"
    )
    recent.add_argument(
        "--state",
        action="append",
        default=[],
        help="filter by observed PR/MR state; repeat for multiple states",
    )
    recent.add_argument("--query", help="search PR/MR identity or descriptive fields")
    recent.add_argument("--offset", type=int, default=0, help="result offset (default: 0)")
    recent.add_argument(
        "--limit", type=int, default=20, help="maximum results to return (default: 20)"
    )

    show = pr_commands.add_parser(
        "show",
        help="show one PR/MR with Workflow, Task, result, batch, and issue history",
        description=(
            "Show all known review context for one PR/MR. By default Task text "
            "results are included. Filters narrow Tasks or recorded findings; "
            "they do not query the provider live."
        ),
        epilog=(
            "Example:\n"
            "  cc-fastapi-admin pr show github org/project 42 "
            "--task-status succeeded"
        ),
    )
    _add_identity_arguments(show)
    show.add_argument("--task-id", help="select one historical Task ID")
    show.add_argument(
        "--task-status",
        action="append",
        default=[],
        help="filter Tasks by status; repeat for multiple statuses",
    )
    show.add_argument(
        "--without-results",
        action="store_true",
        help="omit Task output_text to reduce response size",
    )
    show.add_argument(
        "--severity",
        action="append",
        default=[],
        help="filter findings by severity; repeat for multiple severities",
    )
    show.add_argument(
        "--issue-status",
        action="append",
        default=[],
        help="filter findings by verification status; repeat for multiple statuses",
    )
    show.add_argument(
        "--batch-status",
        action="append",
        default=[],
        help="filter issue batches by status; repeat for multiple statuses",
    )
    show.add_argument("--category", help="filter findings by category")
    show.add_argument("--commit-sha", help="filter findings by reviewed commit SHA")

    collect = pr_commands.add_parser(
        "collect",
        help="record findings from a successful Task for post-merge verification",
        description=(
            "Create a tracked review batch from structured findings extracted from "
            "a successful Agent Task. Without --task-id, the latest active Task "
            "whose Task and Workflow both succeeded is selected."
        ),
        epilog=(
            "Input: {\"issues\": [...]}\n"
            "Example:\n"
            "  cc-fastapi-admin pr collect github org/project 42 --input -"
        ),
    )
    _add_identity_arguments(collect)
    collect.add_argument(
        "--task-id", help="use this successful historical Task instead of auto-selection"
    )
    _add_input_argument(collect)

    add_issues = pr_commands.add_parser(
        "add-issues",
        help="record standalone findings without an Agent Task or Webhook",
        description=(
            "Record structured findings directly against a PR/MR identity. This "
            "does not require a Task or Webhook and does not enter post-merge "
            "verification tracking."
        ),
        epilog=(
            "Input: {\"issues\": [...]} with 1 to 500 issues\n"
            "Example:\n"
            "  cc-fastapi-admin pr add-issues gitea org/project 42 --input -"
        ),
    )
    _add_identity_arguments(add_issues)
    _add_input_argument(add_issues)

    verify = pr_commands.add_parser(
        "verify",
        help="record accepted or not-accepted outcomes for tracked findings",
        description=(
            "Update findings in a tracked batch by issue_no. When more than one "
            "batch is open, --batch-id is required. This command is not valid for "
            "standalone findings created by add-issues."
        ),
        epilog=(
            "Input: {\"results\": [{\"issue_no\": 1, "
            "\"status\": \"accepted\"}]}\n"
            "Example:\n"
            "  cc-fastapi-admin pr verify github org/project 42 --input -"
        ),
    )
    _add_identity_arguments(verify)
    verify.add_argument(
        "--batch-id", help="target batch ID; required when open batches are ambiguous"
    )
    verify.add_argument(
        "--merged-sha", help="merged commit SHA when unavailable from Webhook history"
    )
    _add_input_argument(verify)
    return parser


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "provider", metavar="PROVIDER", help="provider identifier, for example github"
    )
    parser.add_argument(
        "project_path",
        metavar="PROJECT_PATH",
        help="repository path without host, for example org/project",
    )
    parser.add_argument(
        "pr_number", metavar="PR_NUMBER", help="provider PR/MR number or IID"
    )


def _add_input_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input",
        required=True,
        metavar="PATH|-",
        help="JSON input file, or - to read JSON from stdin",
    )


def _run(client: AdminApiClient, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "status":
        return client.status()
    if args.command == "task" and args.task_command == "show":
        return client.show_task(args.task_id)
    if args.pr_command == "recent":
        return client.recent(
            provider=args.provider,
            project_path=args.project_path,
            states=args.state,
            search=args.query,
            offset=args.offset,
            limit=args.limit,
        )
    if args.pr_command == "show":
        return client.show(
            _identity(args),
            task_id=args.task_id,
            task_statuses=args.task_status,
            include_result=not args.without_results,
            severities=args.severity,
            issue_statuses=args.issue_status,
            batch_statuses=args.batch_status,
            category=args.category,
            commit_sha=args.commit_sha,
        )
    stdin_text = sys.stdin.read() if args.input == "-" else None
    payload = read_json_input(args.input, stdin_text)
    if args.pr_command == "collect":
        return client.collect(
            _identity(args),
            task_id=args.task_id,
            issues=parse_collect_input(payload),
        )
    if args.pr_command == "add-issues":
        return client.add_issues(
            _identity(args),
            issues=parse_add_issues_input(payload),
        )
    if args.pr_command == "verify":
        return client.verify(
            _identity(args),
            batch_id=args.batch_id,
            merged_sha=args.merged_sha,
            results=parse_verify_input(payload),
        )
    raise RuntimeError("unknown PR command")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with AdminApiClient(args.base_url, args.token) as client:
            result = _run(client, args)
    except AdminClientError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "exit_code": exc.exit_code},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return exc.exit_code
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
