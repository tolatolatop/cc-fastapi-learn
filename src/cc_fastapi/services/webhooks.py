from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cc_fastapi.db.models import AgentTask, WebhookTrigger
from cc_fastapi.services.queue import TaskQueueService


class WebhookTemplateError(ValueError):
    pass


class WebhookService:
    def __init__(self) -> None:
        self.queue = TaskQueueService()
        self.template_environment = SandboxedEnvironment(
            autoescape=False,
            undefined=StrictUndefined,
        )

    def render_gitlab_prompt(
        self,
        template_path: str,
        *,
        payload: dict[str, Any],
        event_type: str,
        event_uuid: str | None,
        webhook_uuid: str | None,
        instance_url: str | None,
    ) -> str:
        webhook = {
            "provider": "gitlab",
            "event_type": event_type,
            "event_uuid": event_uuid,
            "webhook_uuid": webhook_uuid,
            "instance_url": instance_url,
        }
        context = {
            **payload,
            "payload": payload,
            "event_type": event_type,
            "webhook": webhook,
        }
        try:
            template_source = Path(template_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise WebhookTemplateError(f"failed to load webhook prompt template: {template_path}") from exc
        try:
            prompt = self.template_environment.from_string(template_source).render(context).strip()
        except TemplateError as exc:
            raise WebhookTemplateError(f"failed to render webhook prompt: {exc}") from exc
        if not prompt:
            raise WebhookTemplateError("failed to render webhook prompt: rendered prompt is empty")
        return prompt

    def trigger_gitlab_task(
        self,
        db: Session,
        *,
        payload: dict[str, Any],
        event_type: str,
        event_uuid: str | None,
        webhook_uuid: str | None,
        instance_url: str | None,
        prompt_template_path: str,
        queue_name: str | None,
    ) -> tuple[WebhookTrigger, AgentTask]:
        prompt = self.render_gitlab_prompt(
            prompt_template_path,
            payload=payload,
            event_type=event_type,
            event_uuid=event_uuid,
            webhook_uuid=webhook_uuid,
            instance_url=instance_url,
        )
        task_metadata = {
            "trigger": "gitlab_webhook",
            "gitlab": {
                "event_type": event_type,
                "event_uuid": event_uuid,
                "webhook_uuid": webhook_uuid,
                "instance_url": instance_url,
            },
        }
        task = self.queue.create_task(
            db,
            prompt=prompt,
            model=None,
            queue_name=queue_name,
            metadata=task_metadata,
            priority=0,
            agent_mode=True,
            unattended=True,
            max_attempts=None,
            commit=False,
        )
        trigger = WebhookTrigger(
            provider="gitlab",
            event_type=event_type,
            event_uuid=event_uuid,
            webhook_uuid=webhook_uuid,
            instance_url=instance_url,
            task_id=task.id,
            payload_json=payload,
        )
        db.add(trigger)
        db.commit()
        db.refresh(task)
        db.refresh(trigger)
        return trigger, task

    def list_triggers(self, db: Session, offset: int, limit: int) -> tuple[list[WebhookTrigger], int]:
        items = list(
            db.scalars(
                select(WebhookTrigger)
                .order_by(WebhookTrigger.created_at.desc(), WebhookTrigger.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        total = db.scalar(select(func.count()).select_from(WebhookTrigger)) or 0
        return items, total
