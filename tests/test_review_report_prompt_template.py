from pathlib import Path

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment


def test_incremental_review_report_prompt_template_renders() -> None:
    template_path = Path("config/templates/review_report_incremental_reconciliation_prompt.j2")
    environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    template = environment.from_string(template_path.read_text(encoding="utf-8"))

    prompt = template.render(
        provider="github",
        project_path="fixtures/overlapping-review-reports",
        pr_number=314159,
        source_task_id="report-task-2",
        merged_sha_fallback="a" * 40,
        expected_source_issue_count=4,
        expected_repeated_issue_count=3,
        expected_new_issue_count=1,
        expected_accepted_count=2,
        expected_unverified_count=4,
        expected_total_issue_count=6,
    )

    assert "cc-fastapi-admin task show report-task-2" in prompt
    assert "只新增了 1 个问题" in prompt
    assert "accepted=2、unverified=4" in prompt
    assert "review_task_id=report-task-2 的批次已经存在" in prompt
    assert "{{" not in prompt
