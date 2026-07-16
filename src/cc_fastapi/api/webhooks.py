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
        trigger, task, deduplicated = webhooks.trigger_gitlab_task(
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

    logger.info(
        "gitlab webhook task deduplicated" if deduplicated else "gitlab webhook task created",
        extra={
            "event_type": "gitlab_webhook_task_deduplicated" if deduplicated else "gitlab_webhook_task_created",
            "task_id": task.id,
            "queue_name": task.queue_name,
            "reason": f"webhook_id={trigger.id}",
        },
    )
    return GitLabWebhookResponse(
        webhook_id=trigger.id,
        task_id=task.id,
        status=task.status,
        queue_name=task.queue_name,
        deduplicated=deduplicated,
    )


@router.get("", response_model=WebhookTriggerListResponse, dependencies=[Depends(require_token)])
def list_webhook_triggers(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> WebhookTriggerListResponse:
    items, total = webhooks.list_triggers(db, offset, limit)
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
                payload=item.payload_json,
                created_at=item.created_at,
            )
            for item in items
        ],
        total=total,
    )
