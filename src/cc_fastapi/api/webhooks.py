import logging
from secrets import compare_digest
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from cc_fastapi.api.dependencies import require_token
from cc_fastapi.core.config import get_settings
from cc_fastapi.db.session import get_db
from cc_fastapi.schemas.webhooks import (
    GitLabWebhookResponse,
    WebhookTriggerItemResponse,
    WebhookTriggerListResponse,
    WebhookTriggerListSummaryResponse,
)
from cc_fastapi.services.queue import QueueNotFoundError
from cc_fastapi.services.webhooks import WebhookService, WebhookTemplateError


router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
webhooks = WebhookService()
logger = logging.getLogger(__name__)


def require_gitlab_token(x_gitlab_token: str | None = Header(default=None, alias="X-Gitlab-Token")) -> None:
    expected_token = get_settings().gitlab_webhook_secret
    if not expected_token:
        return
    if x_gitlab_token is None or not compare_digest(x_gitlab_token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid gitlab webhook token")


@router.post("/gitlab", response_model=GitLabWebhookResponse, dependencies=[Depends(require_gitlab_token)])
def receive_gitlab_webhook(
    payload: dict[str, Any],
    x_gitlab_event: str = Header(alias="X-Gitlab-Event"),
    x_gitlab_event_uuid: str | None = Header(default=None, alias="X-Gitlab-Event-UUID"),
    x_gitlab_webhook_uuid: str | None = Header(default=None, alias="X-Gitlab-Webhook-UUID"),
    x_gitlab_instance: str | None = Header(default=None, alias="X-Gitlab-Instance"),
    db: Session = Depends(get_db),
) -> GitLabWebhookResponse:
    settings = get_settings()
    try:
        trigger, task, deduplicated, workflow_run = webhooks.trigger_gitlab_task(
            db,
            payload=payload,
            event_type=x_gitlab_event,
            event_uuid=x_gitlab_event_uuid,
            webhook_uuid=x_gitlab_webhook_uuid,
            instance_url=x_gitlab_instance,
            prompt_template_path=settings.resolved_gitlab_webhook_prompt_template_path,
            queue_name=settings.gitlab_webhook_queue_name or None,
        )
    except WebhookTemplateError as exc:
        logger.warning(
            "gitlab webhook template rendering failed",
            extra={"event_type": "gitlab_webhook_template_failed", "reason": str(exc)},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except QueueNotFoundError as exc:
        logger.warning(
            "gitlab webhook queue resolve failed",
            extra={"event_type": "gitlab_webhook_queue_not_found", "queue_name": exc.queue_name},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    log_event_type = (
        "gitlab_webhook_task_deduplicated"
        if deduplicated
        else "gitlab_webhook_workflow_skipped"
        if task is None
        else "gitlab_webhook_task_created"
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
    return GitLabWebhookResponse(
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
    search: str | None = Query(default=None, alias="q", max_length=200),
    db: Session = Depends(get_db),
) -> WebhookTriggerListResponse:
    items, total = webhooks.list_triggers(db, offset, limit, event_type, search)
    summary_total, event_types = webhooks.summarize_triggers(db)
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
                created_at=item.created_at,
                workflow_run_id=workflow_runs[item.id].id if workflow_runs[item.id] else None,
                workflow_status=workflow_runs[item.id].status if workflow_runs[item.id] else None,
                skip_reason=workflow_runs[item.id].skip_reason if workflow_runs[item.id] else None,
            )
            for item, task_status in items
        ],
        total=total,
        summary=WebhookTriggerListSummaryResponse(total=summary_total, event_types=event_types),
    )
