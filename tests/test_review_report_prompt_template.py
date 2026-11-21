from pathlib import Path

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment


def render_submission_prompt() -> str:
    template_path = Path("config/templates/review_report_submission_prompt.j2")
    environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    template = environment.from_string(template_path.read_text(encoding="utf-8"))

    return template.render(
        provider="github",
        project_path="fixtures/overlapping-review-reports",
        pr_number=314159,
        source_task_id="report-task-2",
        expected_issue_count=4,
    )


def test_review_report_submission_prompt_template_renders() -> None:
    prompt = render_submission_prompt()

    assert "cc-fastapi-admin task show report-task-2" in prompt
    assert "把本次报告的全部 4 个问题作为新的检视批次完整录入" in prompt
    assert "--head-sha 这里替换为被检视版本 sha" in prompt
    assert "--head-committed-at 这里替换为该 commit 的 ISO-8601 提交时间" in prompt
    assert "git show -s --format=%cI" in prompt
    assert "两个版本参数都是必填" in prompt
    assert "review_task_id=report-task-2 的批次此前已由同一份报告创建" in prompt
    assert "{{" not in prompt


def test_review_report_submission_prompt_drops_incremental_deduplication() -> None:
    prompt = render_submission_prompt()

    assert "不要求增量提交" in prompt
    assert "不要读取历史批次做语义比较" in prompt
    assert "不按“根因、影响行为、文件位置”去重" in prompt
    assert "不得重复录入" not in prompt
    assert "只新增" not in prompt
    assert "标记为 accepted" not in prompt
