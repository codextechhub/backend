"""Telling pupils, and their parents, that a notice has reached them.

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
    worker reaches it tells nobody. When the school tells parents
    (``notices.tell_parents``), their parents are told next by
    :func:`tell_parents`; a failure for any one recipient group is logged and
    does not stop the rest.
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
    if tells_parents(notice.tenant):
        tell_parents(notice, context)
    return len(users)


def tells_parents(tenant) -> bool:
    """Whether *tenant* tells parents about new notices (default: yes)."""
    from schools.vs_students.services.rules import resolve_many

    from .constants import CFG_TELL_PARENTS

    found = resolve_many((CFG_TELL_PARENTS,), tenant=tenant)
    value, _ = found.get(CFG_TELL_PARENTS, (True, None))
    return value is not False


def _names(first_names: tuple) -> str:
    """``("Tobi",)`` as "Tobi", two as "Tobi and Ada", more as "A, B and C"."""
    if len(first_names) <= 1:
        return "".join(first_names)
    return ", ".join(first_names[:-1]) + " and " + first_names[-1]


def tell_parents(notice, pupil_context: dict) -> int:
    """Notify each parent with a working account of the children the notice reaches.

    A parent is the ``user`` of a guardian record at this school, active, and
    linked to at least one pupil on the roll in the audience. Each parent is
    told once however many of their children are in the audience, naming them
    all, so parents with the same children share one call. A guardian with no
    account is skipped, and a member of staff who is also a parent is told once,
    as a parent. Returns how many parents were addressed.
    """
    from vs_notifications.notify import send_notification

    from schools.vs_students.models import StudentGuardian

    from .constants import EVENT_PUBLISHED_PARENT
    from .links import parent_app_url
    from .services.notices import audience_pupils

    links = (
        StudentGuardian.all_objects.filter(
            tenant=notice.tenant_id,
            student__in=audience_pupils(notice).values("pk"),
            guardian__tenant=notice.tenant_id,
            guardian__user__isnull=False,
            guardian__user__is_active=True,
            guardian__user__tenant=notice.tenant_id,
        )
        .select_related("guardian__user", "student")
        .order_by("student__date_of_birth", "student__pk")
    )
    children: dict = {}
    parents: dict = {}
    for link in links:
        user = link.guardian.user
        parents[user.pk] = user
        children.setdefault(user.pk, {})[link.student_id] = link.student.first_name

    groups: dict = {}
    for user_id, named in children.items():
        groups.setdefault(tuple(named.values()), []).append(parents[user_id])

    base = {
        "notice_title": notice.title,
        "tenant_name": pupil_context["tenant_name"],
        "notice_url": parent_app_url(notice.tenant, f"notices/{notice.pk}"),
    }
    told = 0
    for first_names, users in groups.items():
        context = {**base, "child_names": _names(first_names)}
        for start in range(0, len(users), BATCH):
            batch = users[start:start + BATCH]
            try:
                send_notification(
                    event_key=EVENT_PUBLISHED_PARENT,
                    context=context,
                    recipients=batch,
                    tenant=notice.tenant,
                    metadata={"notice_id": notice.pk},
                )
            except Exception:
                logger.exception("Notice %s: telling %s parents failed.", notice.pk, len(batch))
                continue
            told += len(batch)
    logger.info("Notice %s told %s parents.", notice.pk, told)
    return told
