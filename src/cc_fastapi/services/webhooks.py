from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cc_fastapi.db.models import AgentTask, TaskStatus, WebhookDeduplicationKey, WebhookTrigger
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

    def _find_existing_trigger(
        self,
        db: Session,
        *,
        provider: str,
        webhook_uuid: str | None,
    ) -> tuple[WebhookTrigger, AgentTask] | None:
        if webhook_uuid is None:
            return None
        trigger = db.scalar(
            select(WebhookTrigger)
            .where(
                WebhookTrigger.provider == provider,
                WebhookTrigger.webhook_uuid == webhook_uuid,
            )
            .order_by(WebhookTrigger.id.asc())
            .limit(1)
        )
        if trigger is None:
            return None
        task = db.get(AgentTask, trigger.task_id)
        if task is None:
            raise RuntimeError(f"webhook trigger references missing task: {trigger.task_id}")
        return trigger, task

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
    ) -> tuple[WebhookTrigger, AgentTask, bool]:
        provider = "gitlab"
        webhook_uuid = webhook_uuid.strip() if webhook_uuid and webhook_uuid.strip() else None
        existing = self._find_existing_trigger(
            db,
            provider=provider,
            webhook_uuid=webhook_uuid,
        )
        if existing is not None:
            return *existing, True

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
            provider=provider,
            event_type=event_type,
            event_uuid=event_uuid,
            webhook_uuid=webhook_uuid,
            instance_url=instance_url,
            task_id=task.id,
            payload_json=payload,
        )
        db.add(trigger)
        if webhook_uuid is not None:
            db.flush()
            db.add(
                WebhookDeduplicationKey(
                    provider=provider,
                    webhook_uuid=webhook_uuid,
                    webhook_trigger_id=trigger.id,
                )
            )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = self._find_existing_trigger(
                db,
                provider=provider,
                webhook_uuid=webhook_uuid,
            )
            if existing is None:
                raise
            return *existing, True
        db.refresh(task)
        db.refresh(trigger)
        return trigger, task, False

    def list_triggers(
        self,
        db: Session,
        offset: int,
        limit: int,
        event_type: str | None = None,
        search: str | None = None,
    ) -> tuple[list[tuple[WebhookTrigger, TaskStatus]], int]:
        query = select(WebhookTrigger, AgentTask.status).join(AgentTask, AgentTask.id == WebhookTrigger.task_id)
        count_query = select(func.count()).select_from(WebhookTrigger)
        filters = []
        if event_type:
            filters.append(WebhookTrigger.event_type == event_type)
        if search and (normalized_search := search.strip()):
            pattern = f"%{normalized_search}%"
            filters.append(
                or_(
                    WebhookTrigger.provider.ilike(pattern),
                    WebhookTrigger.event_type.ilike(pattern),
                    WebhookTrigger.event_uuid.ilike(pattern),
                    WebhookTrigger.webhook_uuid.ilike(pattern),
                    WebhookTrigger.instance_url.ilike(pattern),
                    WebhookTrigger.task_id.ilike(pattern),
                    cast(WebhookTrigger.payload_json, String).ilike(pattern),
                )
            )
        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        items = list(
            db.execute(
                query.order_by(WebhookTrigger.created_at.desc(), WebhookTrigger.id.desc())
                .offset(offset)
                .limit(limit)
            ).tuples()
        )
        total = db.scalar(count_query) or 0
        return items, total

    def summarize_triggers(self, db: Session) -> tuple[int, list[str]]:
        total = db.scalar(select(func.count()).select_from(WebhookTrigger)) or 0
        event_types = list(
            db.scalars(select(WebhookTrigger.event_type).distinct().order_by(WebhookTrigger.event_type.asc()))
        )
        return total, event_types
