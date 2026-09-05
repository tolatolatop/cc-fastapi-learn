from cc_fastapi.core.webhook_payloads import WebhookPayload
from cc_fastapi.workflows.base import WorkflowCorrelationSpec


def change_request_correlation(
    parsed_payload: WebhookPayload | None,
) -> WorkflowCorrelationSpec | None:
    if (
        parsed_payload is None
        or parsed_payload.repository is None
        or parsed_payload.change_request is None
    ):
        return None
    return WorkflowCorrelationSpec(
        provider=parsed_payload.provider,
        resource_type=parsed_payload.change_request.resource_type,
        project_path=parsed_payload.repository.project_path,
        resource_id=parsed_payload.change_request.number,
    )
