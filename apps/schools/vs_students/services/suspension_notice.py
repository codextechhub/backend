"""Telling a pupil's guardians that the pupil has been suspended.

The suspension panel tells a school its guardians are written to, so somebody
has to write to them. The ``student.suspended`` event carries the notice and
``services/status.py`` fires it on a transition into SUSPENDED and on no other
move.

**Who is told is the school's own choice**, held as
``students.suspension.notice`` and read through vs_config's own inheritance, so
a school's value, the platform's and the definition's default layer exactly as
every other setting's do. The default is PRIMARY_GUARDIAN, the narrowest
audience that still tells the family. A school with no stored value therefore
writes to the main contact: the absence of a choice is never read as NOBODY,
because a school that has not opened the screen has not decided to stay silent.
A value stored by hand at the platform layer can be anything the type allows,
so an unrecognised one reads as PRIMARY_GUARDIAN rather than being trusted.

**No adult is written to in the place of another.** Under PRIMARY_GUARDIAN,
a main contact the school holds neither an account nor an email address for
means nothing is sent, and the next guardian on the record is not substituted.
Bright Star suspends Tunde; his record names his mother as main contact with no
address, and his uncle, who shares the house with a landlord the family is in
dispute with. Writing to the uncle because the mother cannot be reached tells
the wrong household a child is in trouble, which is worse than the school
phoning the mother as it would have anyway. The same holds when the setting
says PRIMARY_GUARDIAN and no link is marked primary at all.

**The notice carries the suspension reason only where the person suspending
the pupil chose to send it**, and that choice belongs to the suspension rather
than to the school: the same registrar sends one reason and withholds the
next, because it is this sentence about this child they are weighing. The
default is to withhold.

The reason is free text a member of staff writes for the school's own record,
and it is read on that record by somebody who already holds the context around
it: the pupil's history, the previous incidents, what was said to the parent
on the phone. The same sentence in an email reaches a parent with none of that,
at the worst moment to read it, in a message that can be forwarded, read over
a shoulder or quoted back to the school months later. "Caught stealing again,
third time, the mother is no help" is a defensible note on a record and a
letter no school would choose to send, while "Fighting in the dining hall on
Tuesday" is the sentence that saves the parent a phone call. Withholding by
default costs a family a question they can ask; sending by default costs them a
sentence nobody weighed. Either way the notice says who, from when, when they
are expected back and to contact the school, which is what a parent needs in
order to act.

**A notification backend that is unavailable does not block a suspension.**
Suspending a pupil is the act a school performed and recorded; the message is
a consequence of it. A failure is logged and the status change stands, exactly
as a finance account that cannot be deactivated does not undo a withdrawal.
The alternative is a school unable to suspend a pupil because a mail queue is
down.
"""
from __future__ import annotations

import logging

from django.db import transaction

from vs_config.display import format_date

from ..constants import CFG_SUSPENSION_NOTICE, SuspensionNotice

logger = logging.getLogger(__name__)

EVENT_KEY = "student.suspended"


def read_audience(tenant) -> str:
    """Who *tenant* writes to when a pupil is suspended.

    One of :class:`~schools.vs_students.constants.SuspensionNotice`. With no
    tenant, no active definition or an unrecognised stored value, the main
    contact only.
    """
    from .rules import resolve_many

    if tenant is None:
        return SuspensionNotice.PRIMARY_GUARDIAN.value
    found = resolve_many((CFG_SUSPENSION_NOTICE,), tenant=tenant)
    value, _row = found.get(CFG_SUSPENSION_NOTICE, (None, None))
    if value in SuspensionNotice.values:
        return str(value)
    return SuspensionNotice.PRIMARY_GUARDIAN.value


def guardians_for(student, audience) -> list:
    """The guardians *audience* names on *student*'s record, main contact first.

    Empty under NOBODY, and empty under PRIMARY_GUARDIAN where no link is
    marked primary. One query, with the guardian and their account loaded.
    """
    if audience == SuspensionNotice.NOBODY:
        return []
    links = list(
        student.guardian_links.select_related("guardian", "guardian__user")
        .order_by("-is_primary", "id")
    )
    if audience == SuspensionNotice.PRIMARY_GUARDIAN:
        links = [link for link in links if link.is_primary][:1]
    return [link.guardian for link in links]


def split_audience(guardians) -> tuple[list, list, list]:
    """``(users, addresses, unreachable)`` for a list of guardians.

    A guardian with an account is written to through it, so they read the
    notice in Vision and at the address the account holds; within one school
    one person is one account, so a guardian who also teaches there is already
    in this list and needs no second path. A guardian with only an email
    address is an :class:`UnregisteredRecipient`, which the dispatcher mails
    without writing an in-app row nobody could ever open. A guardian with
    neither is unreachable and is named in the log.
    """
    from vs_notifications.services.dispatch import UnregisteredRecipient

    users, addresses, unreachable = [], [], []
    for guardian in guardians:
        if guardian.user_id:
            users.append(guardian.user)
        elif guardian.email:
            addresses.append(
                UnregisteredRecipient(email=guardian.email, name=guardian.full_name)
            )
        else:
            unreachable.append(guardian)
    return users, addresses, unreachable


def build_context(student, effective_date, *, return_date=None, reason="") -> dict:
    """What the templates read.

    The branch is named only at a school that runs more than one, where it
    tells a parent which site the decision came from; at a school with one it
    repeats what the letterhead already says. ``return_date`` is empty on a
    suspension that stands until a person lifts it, and the templates print
    the line only when it is there, so a family is never told a date the
    school did not set. ``reason`` is empty unless the person suspending the
    pupil chose to send it, and is guarded the same way.
    """
    from vs_rbac.scoping import only_branch_id_or_several

    tenant = student.tenant
    several_branches = only_branch_id_or_several(tenant) is None
    branch_name = student.branch.name if several_branches and student.branch else ""
    return {
        "student_first_name": student.first_name,
        "student_last_name": student.last_name,
        "student_id": student.student_number,
        "branch_name": branch_name,
        "effective_date": format_date(effective_date, tenant, branch=student.branch),
        "return_date": format_date(return_date, tenant, branch=student.branch),
        "reason": (reason or "").strip(),
        "school_name": tenant.name,
    }


def send_suspension_notice(
    student, effective_date, *, return_date=None, reason="",
) -> None:
    """Write to whoever the school chose, or to nobody, and never raise.

    *reason* is what the family is to read, which is empty unless the person
    suspending the pupil chose to send it. The decision is made by the caller,
    so this function cannot widen it.

    Wrapped in a savepoint of its own so a database error inside the
    dispatcher rolls back to it rather than poisoning the transaction the
    suspension is being written in.
    """
    from vs_notifications.services.dispatch import NotificationService

    audience = read_audience(student.tenant)
    if audience == SuspensionNotice.NOBODY:
        return

    guardians = guardians_for(student, audience)
    if not guardians:
        logger.warning(
            "No guardian to tell about student %s's suspension: the school "
            "writes to %s and the record names none.", student.pk, audience,
        )
        return

    users, addresses, unreachable = split_audience(guardians)
    for guardian in unreachable:
        logger.warning(
            "Guardian %s of student %s was not told about the suspension: the "
            "school holds no email address and no account for them, and no "
            "other guardian is written to in their place.",
            guardian.pk, student.pk,
        )
    if not users and not addresses:
        return

    try:
        with transaction.atomic():
            NotificationService.send(
                event_key=EVENT_KEY,
                context=build_context(
                    student, effective_date,
                    return_date=return_date, reason=reason,
                ),
                recipients=users,
                tenant=student.tenant,
                unregistered_recipients=addresses,
            )
    except Exception:
        logger.exception(
            "Suspension notice for student %s could not be dispatched. The "
            "suspension stands.", student.pk,
        )
