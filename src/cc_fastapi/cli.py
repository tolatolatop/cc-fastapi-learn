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
    parse_collect_input,
    parse_verify_input,
    read_json_input,
)


def _identity(args: argparse.Namespace) -> PullRequestIdentity:
    return PullRequestIdentity(args.provider, args.project_path, args.pr_number)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cc-fastapi-admin")
    parser.add_argument(
        "--base-url",
        default=os.getenv("CC_FASTAPI_BASE_URL", ""),
        help="API base URL; defaults to CC_FASTAPI_BASE_URL",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("CC_FASTAPI_TOKEN", ""),
        help=argparse.SUPPRESS,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    pr = commands.add_parser("pr")
    pr_commands = pr.add_subparsers(dest="pr_command", required=True)

    recent = pr_commands.add_parser("recent")
    recent.add_argument("--provider")
    recent.add_argument("--project-path")
    recent.add_argument("--state", action="append", default=[])
    recent.add_argument("--query")
    recent.add_argument("--offset", type=int, default=0)
    recent.add_argument("--limit", type=int, default=20)

    show = pr_commands.add_parser("show")
    _add_identity_arguments(show)
    show.add_argument("--task-id")
    show.add_argument("--task-status", action="append", default=[])
    show.add_argument("--without-results", action="store_true")
    show.add_argument("--severity", action="append", default=[])
    show.add_argument("--issue-status", action="append", default=[])
    show.add_argument("--batch-status", action="append", default=[])
    show.add_argument("--category")
    show.add_argument("--commit-sha")

    collect = pr_commands.add_parser("collect")
    _add_identity_arguments(collect)
    collect.add_argument("--task-id")
    collect.add_argument("--input", required=True)

    verify = pr_commands.add_parser("verify")
    _add_identity_arguments(verify)
    verify.add_argument("--batch-id")
    verify.add_argument("--merged-sha")
    verify.add_argument("--input", required=True)
    return parser


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("provider")
    parser.add_argument("project_path")
    parser.add_argument("pr_number")


def _run(client: AdminApiClient, args: argparse.Namespace) -> dict[str, Any]:
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
