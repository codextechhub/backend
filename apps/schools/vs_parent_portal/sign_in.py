"""Sending a parent to the parent app when they sign in on the school's page.

A parent is the ``user`` of a guardian record at their school. One who holds no
permission there has nothing to do in the staff app, so sign-in names the
parent app (``/parent/``) as their home and the sign-in page sends them there.
A teacher who is also a parent holds permissions and keeps the staff app; the
parent app is still theirs to open at ``/parent/``, and the staff app offers
it to them as one of their ``other_apps`` once a child of theirs is on the
roll, so the way in never opens on an empty app.
"""
from __future__ import annotations


def parent_home_app(user):
    """``/parent/`` for a parent with no staff permission at their school, else None."""
    from schools.vs_notices.constants import PARENT_APP_PATH
    from schools.vs_students.models import Guardian
    from vs_rbac.evaluator import get_effective_permissions

    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is None:
        return None
    if not Guardian.all_objects.filter(tenant_id=tenant_id, user=user).exists():
        return None
    if get_effective_permissions(user, tenant=user.tenant):
        return None
    return PARENT_APP_PATH


def parent_app_beside_staff(user):
    """``/parent/`` for a member of staff who is a parent of a child on the roll, else None.

    The parent's own app is :func:`parent_home_app`'s answer; this is for the
    account that signs in to the staff app and is a parent as well.
    """
    from schools.vs_notices.constants import PARENT_APP_PATH
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import StudentGuardian
    from vs_rbac.evaluator import get_effective_permissions

    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is None:
        return None
    if not StudentGuardian.objects.filter(
        tenant_id=tenant_id, guardian__user=user, student__status__in=PORTAL_STATUSES,
    ).exists():
        return None
    if not get_effective_permissions(user, tenant=user.tenant):
        return None
    return PARENT_APP_PATH


def register() -> None:
    from vs_user.services.home_apps import register_home_app, register_other_app

    register_home_app("parent_app", parent_home_app)
    register_other_app("parent_app", parent_app_beside_staff)
