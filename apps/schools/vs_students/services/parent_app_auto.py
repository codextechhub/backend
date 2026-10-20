"""Invite guardians to the parent app without anybody pressing a button.

At a school with ``guardians.parent_app.auto_access`` on (the default), a
guardian is given the parent app (``guardian_portal_access.issue``) the moment
there is something for it to show them:

* they are linked to a child who is on the roll (``guardians.link``);
* a child of theirs joins the roll, by enrolment, admission, import or
  re-admission (``status.transition``);
* an email is added to a record that had none (``guardians.update_guardian``).

Only a guardian with no parent app account at all is invited. One already
invited or already signed in is left as they are, and one whose access the
school took away is never invited again from here: only a person can give that
back. A guardian whose email belongs to somebody who already works at the
school (a teacher whose child attends) is linked to that account by ``issue``
and sent nothing, because they already have a sign-in.

The invitation is sent after the change commits, so a refused enrolment or a
rolled-back import invites nobody. A guardian ``issue`` refuses (no email, an
email another account holds) is logged and skipped: the change that prompted
the invitation has already been saved, and the school can still give access
by hand from the guardian's record.

The development seeders run inside :func:`paused`, because each one decides
every guardian's access itself: a seeded school whose every guardian was sent
an invitation could not show a guardian with none.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from django.db import transaction

logger = logging.getLogger(__name__)

_state = threading.local()


@contextmanager
def paused():
    """Invite nobody automatically on this thread while the block runs."""
    _state.depth = getattr(_state, "depth", 0) + 1
    try:
        yield
    finally:
        _state.depth -= 1


def invite_when_due(guardians, *, tenant, actor) -> None:
    """Invite each of *guardians* (rows or ids) after commit, where the school's rule says to."""
    from .guardian_rules import read_guardian_rules

    if getattr(_state, "depth", 0):
        return
    ids = sorted({getattr(g, "pk", g) for g in guardians if g is not None})
    if not ids or not read_guardian_rules(tenant).parent_app_auto_access:
        return
    actor_id = getattr(actor, "pk", None)
    transaction.on_commit(lambda: _invite(ids, actor_id))


def _invite(guardian_ids, actor_id) -> None:
    from vs_user.models import User

    from ..exceptions import (
        PortalAccessAlreadyGiven,
        PortalAccountNotUsable,
        PortalEmailInUse,
        PortalGuardianNoChildOnRoll,
        PortalGuardianNoEmail,
    )
    from ..models import Guardian
    from . import guardian_portal_access as access

    refusals = (
        PortalAccessAlreadyGiven, PortalAccountNotUsable, PortalEmailInUse,
        PortalGuardianNoChildOnRoll, PortalGuardianNoEmail,
    )
    actor = User.objects.filter(pk=actor_id).first() if actor_id else None
    for guardian in Guardian.all_objects.filter(pk__in=guardian_ids):
        if not (guardian.email or "").strip() or access.state_of(guardian) != access.NONE:
            continue
        try:
            access.issue(guardian, actor=actor)
        except refusals as refusal:
            logger.info(
                "Parent app not given to guardian %s automatically: %s",
                guardian.pk, refusal,
            )
        except Exception:
            logger.exception(
                "Parent app invitation failed for guardian %s", guardian.pk,
            )
