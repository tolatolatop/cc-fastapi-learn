from pathlib import Path
from urllib.parse import parse_qs

import httpx

from platform_review import CommandResult, GitLabPlatform

MR_URL = "https://gitlab.example.com/group/sub/project/-/merge_requests/17"
HEAD_SHA = "b" * 40
BASE_SHA = "a" * 40
START_SHA = "c" * 40


def mr_payload() -> dict:
    return {
        "iid": 17,
        "title": "Review adapter",
        "state": "opened",
        "source_branch": "feature/review",
        "target_branch": "main",
        "diff_refs": {
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD_SHA,
        },
    }


def response(request: httpx.Request, status: int, payload, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers, request=request)


def test_gitlab_parses_nested_merge_request_url_and_reads_snapshot():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["PRIVATE-TOKEN"] == "secret"
        assert request.url.path == "/api/v4/projects/group/sub/project/merge_requests/17"
        return response(request, 200, mr_payload())

    client = httpx.Client(
        headers={"PRIVATE-TOKEN": "secret"},
        transport=httpx.MockTransport(handler),
    )
    platform = GitLabPlatform(
        "secret",
        client=client,
        allowed_hosts=["gitlab.example.com"],
    )
    ref = platform.parse_pull_request_url(f"{MR_URL}/diffs?view=parallel")
    snapshot = platform.get_pull_request(ref)

    assert ref.project_path == "group/sub/project"
    assert ref.number == 17
    assert ref.url == MR_URL
    assert snapshot.head_sha == HEAD_SHA
    assert snapshot.base_sha == BASE_SHA


def test_gitlab_creates_line_discussion_using_latest_diff_refs():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return response(request, 200, mr_payload())
        form = parse_qs(request.content.decode())
        assert form == {
            "body": ["Possible null dereference"],
            "position[position_type]": ["text"],
            "position[base_sha]": [BASE_SHA],
            "position[start_sha]": [START_SHA],
            "position[head_sha]": [HEAD_SHA],
            "position[old_path]": ["src/service.py"],
            "position[new_path]": ["src/service.py"],
            "position[new_line]": ["42"],
        }
        return response(request, 201, {"id": "discussion-1", "notes": []})

    platform = GitLabPlatform(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    ref = platform.parse_pull_request_url(MR_URL)
    result = platform.create_line_thread(
        ref,
        file_path="src/service.py",
        line=42,
        side="new",
        body="Possible null dereference",
        expected_head_sha=HEAD_SHA,
    )

    assert result["status"] == "succeeded"
    assert result["thread_id"] == "discussion-1"
    assert requests[1].url.path.endswith("/merge_requests/17/discussions")


def test_gitlab_lists_all_pages_and_filters_open_threads():
    def discussion(thread_id: str, *, resolved: bool, path: str) -> dict:
        return {
            "id": thread_id,
            "notes": [
                {
                    "id": 1,
                    "body": "review",
                    "author": {"username": "review-bot"},
                    "resolvable": True,
                    "resolved": resolved,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                    "position": {"new_path": path, "new_line": 8},
                }
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        if page == "1":
            return response(
                request,
                200,
                [discussion("open-1", resolved=False, path="src/a.py")],
                **{"X-Next-Page": "2"},
            )
        return response(
            request,
            200,
            [discussion("resolved-1", resolved=True, path="src/b.py")],
        )

    platform = GitLabPlatform(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    ref = platform.parse_pull_request_url(MR_URL)

    threads = platform.list_threads(ref, status="open", file_path="src/a.py")

    assert [thread.id for thread in threads] == ["open-1"]
    assert threads[0].author == "review-bot"
    assert threads[0].side == "new"


def test_gitlab_replies_and_resolves_discussion():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert parse_qs(request.content.decode())["body"] == ["fixed in latest push"]
            return response(request, 201, {"id": 91, "body": "fixed in latest push"})
        if request.method == "GET":
            return response(
                request,
                200,
                {
                    "id": "thread/1",
                    "notes": [
                        {
                            "id": 1,
                            "resolvable": True,
                            "resolved": False,
                            "body": "issue",
                        }
                    ],
                },
            )
        assert parse_qs(request.content.decode()) == {"resolved": ["true"]}
        return response(request, 200, {"id": "thread/1", "notes": []})

    platform = GitLabPlatform(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    ref = platform.parse_pull_request_url(MR_URL)

    reply = platform.reply_thread(ref, "thread/1", "fixed in latest push")
    resolved = platform.resolve_thread(ref, "thread/1")

    assert reply["comment_id"] == 91
    assert resolved["status"] == "succeeded"
    assert calls == [
        (
            "POST",
            "/api/v4/projects/group/sub/project/merge_requests/17/discussions/thread/1/notes",
        ),
        (
            "GET",
            "/api/v4/projects/group/sub/project/merge_requests/17/discussions/thread/1",
        ),
        (
            "PUT",
            "/api/v4/projects/group/sub/project/merge_requests/17/discussions/thread/1",
        ),
    ]


def test_gitlab_checkout_uses_merge_request_head_ref(tmp_path: Path):
    commands: list[list[str]] = []
    destination = tmp_path / "checkout"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/merge_requests/17"):
            return response(request, 200, mr_payload())
        return response(
            request,
            200,
            {"http_url_to_repo": "https://gitlab.example.com/group/sub/project.git"},
        )

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["env"]["PLATFORM_GIT_TOKEN"] == "secret"
        assert Path(kwargs["env"]["GIT_ASKPASS"]).is_file()
        if command[:2] == ["git", "clone"]:
            destination.mkdir()
        stdout = f"{HEAD_SHA}\n" if command[-2:] == ["rev-parse", "HEAD"] else ""
        return CommandResult(returncode=0, stdout=stdout)

    platform = GitLabPlatform(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        command_runner=run,
    )
    ref = platform.parse_pull_request_url(MR_URL)

    result = platform.prepare_checkout(ref, destination)

    assert result["workspace_path"] == str(destination)
    assert result["head_sha"] == HEAD_SHA
    assert any(f"refs/merge-requests/{ref.number}/head" in command for command in commands)
    assert commands[-2][-2:] == ["--detach", HEAD_SHA]


def test_csv_batch_returns_row_level_failures_without_stopping(tmp_path: Path):
    csv_path = tmp_path / "operations.csv"
    csv_path.write_text(
        "operation_id,action,thread_id,file_path,line,side,body,head_sha\n"
        "op-1,unknown,,,,,,,\n"
        "op-2,resolve,thread-2,,,,,\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return response(
            request,
            200,
            {
                "id": "thread-2",
                "notes": [{"id": 1, "resolvable": True, "resolved": True}],
            },
        )

    platform = GitLabPlatform(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    ref = platform.parse_pull_request_url(MR_URL)

    result = platform.apply_csv(ref, csv_path)

    assert result["failed"] == 1
    assert result["skipped"] == 1
    assert result["results"][0]["row"] == 2
    assert result["results"][1]["thread_id"] == "thread-2"
