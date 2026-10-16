"""Telling pupils a notice has reached them.

Publishing commits first and the telling follows, so the worker can always find
the notice it was sent, and a publication that rolls back tells nobody.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction

logger = logging.getLogger(__name__)

#: Recipients per call, so a school-wide notice to nine hundred pupils is a few
#: calls rather than one enormous one.
BATCH = 200


def queue_published(notice_id: int) -> None:
    """Dispatch the notification for *notice_id* once the caller commits."""
    transaction.on_commit(lambda: dispatch_published.delay(notice_id))


@shared_task(name="vs_notices.dispatch_published")
def dispatch_published(notice_id: int) -> int:
    """Notify every pupil in the audience who has a working portal account.

    Returns how many pupils were addressed. A notice withdrawn before the
    worker reaches it tells nobody.
    """
    from vs_notifications.notify import send_notification

    from .constants import EVENT_PUBLISHED, NoticeStatus
    from .models import Notice
    from .services.notices import audience_label, audience_pupils

    notice = Notice.all_objects.select_related("tenant").filter(pk=notice_id).first()
    if notice is None or notice.status != NoticeStatus.PUBLISHED:
        return 0
    users = [
        pupil.user
        for pupil in audience_pupils(notice)
        .filter(user__isnull=False, user__is_active=True)
        .select_related("user")
    ]
    profile = getattr(notice.tenant, "school_profile", None)
    context = {
        "notice_title": notice.title,
        "notice_body": notice.body,
        "notice_excerpt": notice.body[:160] + ("..." if len(notice.body) > 160 else ""),
        "audience_label": audience_label(notice),
        "tenant_name": getattr(profile, "name", "") or notice.tenant.name,
    }
    for start in range(0, len(users), BATCH):
        send_notification(
            event_key=EVENT_PUBLISHED,
            context=context,
            recipients=users[start:start + BATCH],
            tenant=notice.tenant,
            metadata={"notice_id": notice.pk},
        )
    logger.info("Notice %s told %s pupils.", notice.pk, len(users))
    return len(users)
