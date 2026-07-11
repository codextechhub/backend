"""Celery tasks for vs_workflow."""
import logging
from typing import Optional
from celery import shared_task
from vs_workflow.constants import NOTIF_EVENT_KEYS

logger = logging.getLogger(__name__)

# Send workflow lifecycle notifications without blocking the workflow action.
@shared_task(name="vs_workflow.dispatch_notification")
def dispatch_notification(*, instance_id: str, event_key: str,
                           recipient_user_ids: list, context: Optional[dict]=None) -> None:
    # Ignore unknown event keys so callers cannot emit arbitrary notification types.
    if event_key not in NOTIF_EVENT_KEYS:
        return
    from vs_workflow.models import WorkflowInstance
    try:
        instance = WorkflowInstance.objects.select_related("template").get(pk=instance_id)
    except WorkflowInstance.DoesNotExist:
        return
    # Template opt-in: an untouched template ({} — never configured) notifies
    # for every wired event; once the author has configured ANY key, the dict
    # is exact intent and missing keys mean off.
    events = instance.template.notification_events or {}
    if events and not events.get(event_key, False):
        return

    from django.contrib.auth import get_user_model
    recipients = list(get_user_model().objects.filter(pk__in=recipient_user_ids))
    if not recipients:
        return
    try:
        # Notification dispatch is best-effort; the workflow state is already committed.
        from vs_notifications.services.dispatch import NotificationService
        NotificationService.send(
            event_key=event_key,
            recipients=recipients,
            school=instance.school,
            context={"workflow_instance_id": str(instance.id),
                     "document_type": instance.document_type,
                     "document_id": instance.document_object_id, **(context or {})})
    except ImportError:
        logger.info("vs_notifications not installed; notification skipped for %s", event_key)
    except Exception:
        logger.exception("Notification dispatch failed for event %s", event_key)
