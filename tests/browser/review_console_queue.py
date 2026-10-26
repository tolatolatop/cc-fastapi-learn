import asyncio
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
ART = ROOT / ".artifacts"
ART.mkdir(exist_ok=True)
now = "2026-10-26T03:00:00Z"
repos = [
    {
        "provider": "github",
        "project_path": "team/service",
        "permission": "write",
        "issue_total": 3100,
        "pending_total": 2000,
    }
]
user = {
    "id": "reviewer",
    "username": "reviewer",
    "display_name": "审核员",
    "is_admin": True,
    "is_active": True,
    "auth_source": "local",
    "created_at": now,
    "grants": [],
}


def pr(number):
    total = 35 if str(number) == "42" else 61
    reviewed = (total + 3) // 4
    return {
        "provider": "github",
        "project_path": "team/service",
        "pr_number": str(number),
        "pr_url": f"https://github.example/team/service/pull/{number}",
        "completion_status": "pending",
        "batch_total": 2,
        "issue_total": total,
        "reviewed_total": reviewed,
        "pending_total": total - reviewed,
        "updated_at": now,
    }


def issue(index, number):
    return {
        "id": f"{number}-{index}",
        "batch_id": "batch",
        "provider": "github",
        "project_path": "team/service",
        "pr_number": str(number),
        "pr_url": pr(number)["pr_url"],
        "issue_no": index + 1,
        "severity": "high" if index % 3 == 0 else "medium",
        "category": "correctness",
        "title": f"重试路径可能重复提交订单 · 意见 {index + 1}",
        "description": "### 问题说明\n重试请求没有复用幂等键，网络超时后可能重复写入。\n"
        + "需要在请求边界保留幂等键并验证写入结果。\n" * 80
        + "\n**完整内容末尾**",
        "file_path": f"src/services/orders/retry_handler_{index}.py",
        "line_number": 12 + index,
        "status": "accepted" if index % 4 == 0 else "unverified",
        "reason_code": None,
        "note": None,
        "decided_by_id": None,
        "decided_by_name": None,
        "decided_at": None,
        "verification_status": "unverified",
        "review_head_sha": "abcdef0123456789",
        "merged_sha": None,
        "batch_status": "completed",
        "batch_created_at": now,
        "batch_extracted_at": now,
        "batch_verified_at": None,
        "created_at": now,
        "updated_at": now,
    }


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def main(port):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(
            viewport={"width": 1440, "height": 900}, device_scale_factor=1
        )
        requests = []
        errors = []
        changes = {}
        page.on("pageerror", lambda err: errors.append(str(err)))

        async def route(r):
            u = urlparse(r.request.url)
            q = parse_qs(u.query)
            requests.append((u.path, q))
            offset = int(q.get("offset", [0])[0])
            limit = int(q.get("limit", [25])[0])
            if u.path.endswith("/auth/me"):
                data = user
            elif u.path.endswith("/repositories"):
                data = {"items": repos}
            elif u.path.endswith("/queue"):
                search = q.get("q", [""])[0]
                if search == "error":
                    await r.fulfill(status=502, json={"detail": "队列暂不可用"})
                    return
                values = [pr(n) for n in range(3001, 0, -1)]
                if search:
                    values = [
                        v
                        for v in values
                        if all(
                            k.lower()
                            in f"{v['project_path']} #{v['pr_number']} {v['pr_url']}".lower()
                            for k in search.split()
                        )
                    ]
                data = {"items": values[offset : offset + limit], "total": len(values)}
            elif u.path.endswith("/pull-request"):
                if q["pr_number"] == ["77"]:
                    await asyncio.sleep(0.8)
                data = pr(q["pr_number"][0])
            elif u.path.endswith("/issues"):
                number = q["pr_number"][0]
                if q.get("q") == ["slow"]:
                    await asyncio.sleep(0.6)
                values = [
                    changes.get(f"{number}-{i}", issue(i, number))
                    for i in range(35 if number == "42" else 61)
                ]
                if q.get("status"):
                    values = [v for v in values if v["status"] in q["status"]]
                if q.get("q"):
                    search = q["q"][0]
                    values = [
                        v
                        for v in values
                        if search in v["title"] or search in v["description"]
                    ]
                if q.get("sort") == ["pending_first"]:
                    values.sort(key=lambda v: v["status"] != "unverified")
                data = {"items": values[offset : offset + limit], "total": len(values)}
            elif u.path.endswith("/status"):
                ident = u.path.split("/")[-2]
                number, index = ident.split("-")
                data = {
                    **issue(int(index), number),
                    **r.request.post_data_json,
                    "updated_at": "2026-10-26T04:00:00Z",
                }
                changes[ident] = data
            elif u.path.endswith("/history"):
                data = {"items": []}
            else:
                raise AssertionError(u.path)
            await r.fulfill(json=data)

        await page.route("**/api/**", route)
        await page.goto(f"http://127.0.0.1:{port}")
        await expect(page.locator(".pr-rail-list > button")).to_have_count(25)
        await expect(page.locator(".compact-issue-card")).to_have_count(25)
        assert len([r for r in requests if r[0].endswith("/queue")]) == 1
        assert len([r for r in requests if r[0].endswith("/issues")]) == 1
        assert all(
            int(q["limit"][0]) == 25
            for path, q in requests
            if path.endswith(("/queue", "/issues"))
        )
        assert await page.evaluate(
            "document.documentElement.scrollHeight <= innerHeight + 1"
        )
        assert await page.locator(".pr-rail-list").evaluate(
            "(e) => e.scrollHeight > e.clientHeight"
        )
        assert await page.locator(".issue-preview").first.evaluate(
            "(e) => e.clientHeight < 45"
        )
        await page.screenshot(path=str(ART / "queue-desktop.png"), full_page=True)
        queue_nav = page.get_by_role("navigation", name="审核队列分页")
        await queue_nav.get_by_role("button", name="下一页").click()
        await expect(page.locator(".pr-rail-list > button").first).to_contain_text(
            "#2976"
        )
        await expect(page.get_by_role("heading", name="PR / MR #3001")).to_be_visible()
        await page.get_by_label("搜索审核队列").fill("#42")
        await expect(page.locator(".pr-rail-list > button")).to_have_count(11)
        await page.get_by_label("搜索审核队列").fill("error")
        await expect(page.get_by_role("alert")).to_contain_text("队列暂不可用")
        await page.get_by_role("button", name="重试", exact=True).click()
        await expect(page.get_by_role("alert")).to_contain_text("队列暂不可用")
        await page.get_by_label("搜索审核队列").fill("")
        await expect(page.locator(".pr-rail-list > button")).to_have_count(25)
        issue_nav = page.get_by_role("navigation", name="检视意见分页")
        await issue_nav.get_by_role("button", name="下一页").click()
        await expect(page.locator(".issue-opinion > strong").first).to_contain_text(
            "意见 26"
        )
        await page.get_by_label("意见状态", exact=True).select_option("unverified")
        await expect(page.locator(".issue-opinion > strong").first).to_contain_text(
            "意见 2"
        )
        await expect(issue_nav.locator(".active")).to_contain_text("1 / 2")
        await (
            page.locator(".compact-issue-card")
            .first.get_by_role("button", name="接受", exact=True)
            .click()
        )
        await expect(page.locator(".issue-opinion > strong").first).to_contain_text(
            "意见 3"
        )
        await page.get_by_label("意见状态", exact=True).select_option("")
        await expect(page.locator(".compact-issue-card")).to_have_count(25)
        await (
            page.locator(".compact-issue-card")
            .first.get_by_role("button", name="详情")
            .click()
        )
        await expect(
            page.get_by_role("dialog").locator(".issue-markdown")
        ).to_contain_text("完整内容末尾")
        await page.get_by_role("dialog").get_by_role("button", name="Close").click()
        # A slower old search must not replace the newer filter result.
        async with page.expect_request("**/api/v1/issues?**"):
            await page.get_by_label("搜索检视意见").fill("slow")
        await page.get_by_label("搜索检视意见").fill("意见 61")
        await expect(page.locator(".compact-issue-card")).to_have_count(1)
        await expect(page.locator(".issue-opinion > strong")).to_contain_text("意见 61")
        await page.get_by_label("搜索检视意见").fill("")
        await expect(page.locator(".compact-issue-card")).to_have_count(25)
        await page.goto(
            f"http://127.0.0.1:{port}/?provider=github&project_path=team%2Fservice&pr=42"
        )
        await expect(page.get_by_role("heading", name="PR / MR #42")).to_be_visible()
        await expect(page.locator(".compact-issue-card")).to_have_count(25)
        await expect(page.locator(".pr-rail-list > button").first).to_contain_text(
            "#3001"
        )
        await page.locator(".pr-rail-list > button").first.click()
        await expect(page.get_by_role("heading", name="PR / MR #3001")).to_be_visible()
        await page.go_back()
        await expect(page.get_by_role("heading", name="PR / MR #42")).to_be_visible()
        # A slow deep-link lookup must not override a newer explicit selection.
        await page.goto(
            f"http://127.0.0.1:{port}/?provider=github&project_path=team%2Fservice&pr=77"
        )
        await expect(page.locator(".pr-rail-list > button")).to_have_count(25)
        await page.locator(".pr-rail-list > button").first.click()
        await page.wait_for_timeout(900)
        await expect(page.get_by_role("heading", name="PR / MR #3001")).to_be_visible()
        await page.goto(
            f"http://127.0.0.1:{port}/?provider=github&project_path=team%2Fservice&pr=42"
        )
        await expect(page.locator(".compact-issue-card")).to_have_count(25)
        # A removed last-page item clamps to the new final page.
        await page.get_by_label("意见状态", exact=True).select_option("unverified")
        await expect(page.locator(".compact-issue-card")).to_have_count(25)
        await issue_nav.get_by_role("button", name="最后一页").click()
        await expect(page.locator(".compact-issue-card")).to_have_count(1)
        await (
            page.locator(".compact-issue-card")
            .get_by_role("button", name="接受", exact=True)
            .click()
        )
        await expect(page.locator(".compact-issue-card")).to_have_count(25)
        await expect(issue_nav.locator(".active")).to_contain_text("1 / 1")
        for width in [1024, 768, 320, 390]:
            await page.set_viewport_size({"width": width, "height": 844})
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            ), width
        await page.get_by_label("意见状态", exact=True).select_option("")
        await expect(page.locator(".compact-issue-card")).to_have_count(25)
        await page.screenshot(path=str(ART / "queue-mobile.png"), full_page=False)
        assert not errors, errors
        print(
            json.dumps(
                {
                    "browser": "passed",
                    "requests": len(requests),
                    "initial_queue_requests": 1,
                    "page_size": 25,
                    "viewports": [1440, 1024, 768, 320, 390],
                    "screenshots": ["queue-desktop.png", "queue-mobile.png"],
                }
            )
        )
        await browser.close()


server = ThreadingHTTPServer(
    ("127.0.0.1", 0),
    functools.partial(Handler, directory=str(ROOT / "review-console/frontend/dist")),
)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    asyncio.run(main(server.server_port))
finally:
    server.shutdown()
    server.server_close()
    thread.join()
