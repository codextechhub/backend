"""How a parent reaches the parent app, and how long they stay signed in to it.

A parent is the ``user`` of a guardian record at their school. One who holds no
permission there has nothing to do in the staff app, so they are offered a
**device session** in the parent app (:mod:`vs_user.services.device_sessions`):
signed in for the school's ``parents.portal.session_days`` between uses, and a
browser sign-in on the school's page answers ``device_app`` ``/parent/`` so the
page sends them there.

A teacher who is also a parent holds permissions and keeps the staff app. They
are offered the same device session in the parent app once a child of theirs is
on the roll, so the parent app (and the store app built from it) can sign them
in, but their policy does not send a browser sign-in there: the staff app stays
their sign-in, offers the parent app as one of their ``other_apps``, and the
pushes they subscribed to from the staff app still open the staff app.

An account that is a pupil's own sign-in is never answered here, so a pupil
keeps the student app's policy whatever else is linked to the account.
"""
from __future__ import annotations

import datetime as dt


def _is_guardian(user) -> bool:
    from schools.vs_students.models import Guardian

    return Guardian.all_objects.filter(tenant_id=user.tenant_id, user=user).exists()


def _has_child_on_roll(user) -> bool:
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import StudentGuardian

    return StudentGuardian.objects.filter(
        tenant_id=user.tenant_id, guardian__user=user, student__status__in=PORTAL_STATUSES,
    ).exists()


def _holds_staff_permission(user) -> bool:
    from vs_rbac.evaluator import get_effective_permissions

    return bool(get_effective_permissions(user, tenant=user.tenant))


def parent_home_app(user):
    """``/parent/`` for a parent with no staff permission at their school, else None."""
    from schools.vs_notices.constants import PARENT_APP_PATH

    if getattr(user, "tenant_id", None) is None or not _is_guardian(user):
        return None
    if _holds_staff_permission(user):
        return None
    return PARENT_APP_PATH


def parent_app_beside_staff(user):
    """``/parent/`` for a member of staff who is a parent of a child on the roll, else None.

    The parent's own app is :func:`parent_home_app`'s answer; this is for the
    account that signs in to the staff app and is a parent as well.
    """
    from schools.vs_notices.constants import PARENT_APP_PATH

    if getattr(user, "tenant_id", None) is None or not _has_child_on_roll(user):
        return None
    if not _holds_staff_permission(user):
        return None
    return PARENT_APP_PATH


def portal_session_days(tenant) -> int:
    """How many days this school's parents stay signed in between uses.

    A stored value outside 1 to 90, or not a whole number, falls back to the
    default.
    """
    from schools.vs_students.services.rules import resolve_many

    from .constants import CFG_PORTAL_SESSION_DAYS, PORTAL_SESSION_DAYS_DEFAULT

    value, _ = resolve_many((CFG_PORTAL_SESSION_DAYS,), tenant=tenant).get(
        CFG_PORTAL_SESSION_DAYS, (PORTAL_SESSION_DAYS_DEFAULT, None),
    )
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 90:
        return PORTAL_SESSION_DAYS_DEFAULT
    return value


def parent_device_session(user):
    """The parent app's device session for a guardian's account, else None.

    A parent with no staff permission is offered it with their browser sign-in
    sent to the app. A member of staff who is a parent of a child on the roll
    is offered it with the browser sign-in left where it is. A pupil's account,
    a member of staff with no child on the roll and anyone who is no guardian
    are not answered.
    """
    from schools.vs_notices.constants import PARENT_APP_PATH
    from schools.vs_students.models import Student

    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is None:
        return None
    if Student.all_objects.filter(user=user, tenant_id=tenant_id).exists():
        return None
    if not _is_guardian(user):
        return None
    if not _holds_staff_permission(user):
        sends_browser_sign_in = True
    elif _has_child_on_roll(user):
        sends_browser_sign_in = False
    else:
        return None

    from vs_user.services.device_sessions import DeviceSessionPolicy

    return DeviceSessionPolicy(
        lifetime=dt.timedelta(days=portal_session_days(user.tenant)),
        app_path=PARENT_APP_PATH,
        sends_browser_sign_in_to_app=sends_browser_sign_in,
    )


def register() -> None:
    from vs_user.services.device_sessions import register_device_session_policy
    from vs_user.services.home_apps import register_home_app, register_other_app

    register_home_app("parent_app", parent_home_app)
    register_other_app("parent_app", parent_app_beside_staff)
    register_device_session_policy("parent_app", parent_device_session)
