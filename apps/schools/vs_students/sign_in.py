"""How a pupil signs in, and who else hears about their account.

A pupil's account is an ordinary school account linked to their record
(``Student.user``). The account engine knows nothing about pupils; this app
tells it two things from ``AppConfig.ready()`` through
:mod:`vs_user.services.sign_in_identifiers`:

* an **admission number** names the pupil's account in the sign-in box and on
  the forgot-password page, exactly as a staff number names an employee's;
* an invitation or password-reset link for a pupil's account also goes to the
  pupil's **primary guardian**, because a pupil may have no email of their own,
  and a parent is who helps a child through activation either way;
* a pupil's account is offered a **device session** in the student app
  (:mod:`vs_user.services.device_sessions`): signed in for the school's
  ``students.portal.session_days`` between uses, several pupils side by side
  on one family phone.
"""
from __future__ import annotations


def admission_number_resolver(*, tenant_id, identifier):
    """The accounts whose pupil record carries *identifier* inside *tenant_id*.

    Case is ignored, as the stored uniqueness (``uq_student_tenant_number_ci``)
    ignores it. Only a pupil who has been given an account is a match, so an
    admission number belonging to a pupil without one answers as unknown. At
    most two rows are read, enough for the engine to see an ambiguous number.
    """
    from .models import Student

    number = (identifier or "").strip()
    if not number:
        return []
    rows = (
        Student.all_objects.select_related("user__tenant")
        .filter(
            tenant_id=tenant_id, student_number__iexact=number,
            user__isnull=False,
        )[:2]
    )
    return [row.user for row in rows]


def guardian_delivery_addresses(user):
    """The primary guardian's address, for an account that belongs to a pupil."""
    from vs_user.services.sign_in_identifiers import DeliveryAddress

    from .models import StudentGuardian

    if not user.pk:
        return []
    link = (
        StudentGuardian.objects.select_related("guardian")
        .filter(student__user=user, student__tenant_id=user.tenant_id, is_primary=True)
        .first()
    )
    if link is None or not link.guardian.email:
        return []
    return [DeliveryAddress(email=link.guardian.email, name=link.guardian.full_name)]


def admission_number_label(user):
    """The admission number a pupil's account signs in with, or None."""
    from .models import Student

    return (
        Student.all_objects.filter(user=user, tenant_id=user.tenant_id)
        .exclude(student_number="")
        .values_list("student_number", flat=True)
        .first()
    )


def portal_session_days(tenant) -> int:
    """How many days this school's pupils stay signed in between uses."""
    from .constants import CFG_PORTAL_SESSION_DAYS, PORTAL_SESSION_DAYS_DEFAULT
    from .services.rules import resolve_many

    value, _ = resolve_many((CFG_PORTAL_SESSION_DAYS,), tenant=tenant).get(
        CFG_PORTAL_SESSION_DAYS, (PORTAL_SESSION_DAYS_DEFAULT, None),
    )
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 90:
        return PORTAL_SESSION_DAYS_DEFAULT
    return value


def pupil_device_session(user):
    """The student app's device session, for an account linked to a pupil.

    A pupil stays signed in to the student app for the school's
    ``students.portal.session_days`` between uses; any other account is
    answered by nobody here and keeps the browser session.
    """
    import datetime as dt

    from vs_user.services.device_sessions import DeviceSessionPolicy

    from .constants import STUDENT_APP_PATH
    from .models import Student

    if not Student.all_objects.filter(user=user, tenant_id=user.tenant_id).exists():
        return None
    return DeviceSessionPolicy(
        lifetime=dt.timedelta(days=portal_session_days(user.tenant)),
        app_path=STUDENT_APP_PATH,
    )


def register():
    """Tell the account engine this app issues admission numbers and keeps pupils signed in."""
    from vs_user.services.device_sessions import register_device_session_policy
    from vs_user.services.sign_in_identifiers import (
        register_delivery_address_provider,
        register_identifier_label_provider,
        register_identifier_resolver,
    )

    register_identifier_resolver("admission_number", admission_number_resolver)
    register_identifier_label_provider("admission_number", admission_number_label)
    register_delivery_address_provider("pupil_primary_guardian", guardian_delivery_addresses)
    register_device_session_policy("student_app", pupil_device_session)
