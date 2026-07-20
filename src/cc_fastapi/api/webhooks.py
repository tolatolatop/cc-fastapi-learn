import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.orm import Session

from cc_fastapi.api.dependencies import require_token
from cc_fastapi.core.config import get_settings
from cc_fastapi.core.webhook_payloads import WebhookPayload
from cc_fastapi.core.webhook_providers import (
    WebhookRequestError,
    webhook_provider_registry,
)
from cc_fastapi.db.session import get_db
from cc_fastapi.schemas.webhooks import (
    WebhookPayloadResponse,
    WebhookResponse,
    WebhookTriggerItemResponse,
    WebhookTriggerListResponse,
    WebhookTriggerListSummaryResponse,
)
from cc_fastapi.services.queue import QueueNotFoundError
from cc_fastapi.services.webhooks import WebhookService, WebhookTemplateError


router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
webhooks = WebhookService()
logger = logging.getLogger(__name__)


def _webhook_payload_response(
    provider: str,
    event_type: str,
    payload: dict[str, Any],
) -> WebhookPayloadResponse | None:
    parsed_payload = WebhookPayload.from_payload(provider, event_type, payload)
    if parsed_payload is None:
        return None
    return WebhookPayloadResponse.model_validate(parsed_payload)


@router.post("/{provider}", response_model=WebhookResponse)
async def receive_provider_webhook(
    request: Request,
    provider: str = Path(max_length=32),
    db: Session = Depends(get_db),
) -> WebhookResponse:
    definition = webhook_provider_registry.get(provider)
    if definition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="webhook provider is not registered",
        )
    raw_body = await request.body()
    try:
        received = definition.request_adapter.parse(
            request.headers,
            raw_body,
            get_settings(),
        )
    except WebhookRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    settings = get_settings()
    try:
        trigger, task, deduplicated, workflow_run = webhooks.trigger_task(
            db,
            provider=definition.id,
            payload=received.payload,
            event_type=received.event_type,
            event_uuid=received.event_uuid,
            webhook_uuid=received.webhook_uuid,
            instance_url=received.instance_url,
            prompt_template_path=definition.prompt_template_path(settings),
            queue_name=definition.queue_name(settings),
            provider_metadata=received.provider_metadata,
        )
    except WebhookTemplateError as exc:
        logger.warning(
            "webhook template rendering failed",
            extra={
                "event_type": f"{definition.id}_webhook_template_failed",
                "reason": str(exc),
            },
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except QueueNotFoundError as exc:
        logger.warning(
            "webhook queue resolve failed",
            extra={
                "event_type": f"{definition.id}_webhook_queue_not_found",
                "queue_name": exc.queue_name,
            },
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    log_event_type = (
        f"{definition.id}_webhook_task_deduplicated"
        if deduplicated
        else f"{definition.id}_webhook_workflow_skipped"
        if task is None
        else f"{definition.id}_webhook_task_created"
    )
    logger.info(
        log_event_type.replace("_", " "),
        extra={
            "event_type": log_event_type,
            "task_id": task.id if task else None,
            "queue_name": task.queue_name if task else None,
            "reason": f"webhook_id={trigger.id}",
        },
    )
    return WebhookResponse(
        webhook_id=trigger.id,
        task_id=task.id if task else None,
        status=task.status if task else None,
        queue_name=task.queue_name if task else None,
        deduplicated=deduplicated,
        workflow_run_id=workflow_run.id,
        workflow_status=workflow_run.status,
        skip_reason=workflow_run.skip_reason,
    )


@router.get("", response_model=WebhookTriggerListResponse, dependencies=[Depends(require_token)])
def list_webhook_triggers(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    event_type: str | None = Query(default=None, max_length=128),
    provider: str | None = Query(default=None, max_length=32),
    search: str | None = Query(default=None, alias="q", max_length=200),
    db: Session = Depends(get_db),
) -> WebhookTriggerListResponse:
    items, total = webhooks.list_triggers(db, offset, limit, event_type, search, provider)
    summary_total, event_types, providers = webhooks.summarize_triggers(db)
    workflow_runs = {item.id: webhooks.get_workflow_run(db, item.id) for item, _task_status in items}
    return WebhookTriggerListResponse(
        items=[
            WebhookTriggerItemResponse(
                id=item.id,
                provider=item.provider,
                event_type=item.event_type,
                event_uuid=item.event_uuid,
                webhook_uuid=item.webhook_uuid,
                instance_url=item.instance_url,
                task_id=item.task_id,
                task_status=task_status,
                payload=item.payload_json,
                parsed_payload=_webhook_payload_response(
                    item.provider,
                    item.event_type,
                    item.payload_json,
                ),
                created_at=item.created_at,
                workflow_run_id=workflow_runs[item.id].id if workflow_runs[item.id] else None,
                workflow_status=workflow_runs[item.id].status if workflow_runs[item.id] else None,
                skip_reason=workflow_runs[item.id].skip_reason if workflow_runs[item.id] else None,
            )
            for item, task_status in items
        ],
        total=total,
        summary=WebhookTriggerListSummaryResponse(
            total=summary_total,
            event_types=event_types,
            providers=providers,
        ),
    )
