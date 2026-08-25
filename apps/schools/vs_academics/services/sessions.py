"""The school year: activation, narrowing, archiving and the term rules.

Activation is the one operation in this module that can rewrite rows it was not
asked about, so all of it happens in one transaction under one lock. FRD v2.6
FR-013 is the specification; the short version is that activating a session
takes every branch it covers away from whatever covered them before, rather
than colliding with it.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..exceptions import (
    SessionArchivedReadOnly,
    SessionHasArchivedTerm,
    TermDatesOverlap,
    TermOrderConflict,
    TermOutsideSession,
    TermSessionNotDraft,
)
from ..models import AcademicSession, AcademicTerm, SessionBranch, SessionStatus


def assert_writable(session):
    """The FR-009 guard: refuse every ordinary write against an archived year.

    Called by every serializer and service that writes a session or a term, and
    never reimplemented in a view - a guard written twice is a guard that will
    be applied once.

    Activation deliberately does not call this. Clearing ``archived_at`` is the
    act of *leaving* the archived state rather than an edit made while inside
    it, and a guard that refused it would make the correction path unreachable.
    """
    if session.status == SessionStatus.ARCHIVED:
        raise SessionArchivedReadOnly()


def covered_branch_ids(session, tenant):
    """Which branches this session applies to, resolved.

    A session that names no branches applies to the whole school, so it covers
    every branch the tenant has *now* - computed rather than stored, which is
    the point. Materialising the set would freeze "the whole school" to the
    branches that existed the day the session was written, and a branch opening
    in January would sit outside the running year.
    """
    from vs_tenants.models import Branch

    named = set(
        SessionBranch.all_objects
        .filter(session=session)
        .values_list("branch_id", flat=True)
    )
    if named:
        return named
    return set(
        Branch.all_objects.filter(tenant=tenant).values_list("id", flat=True)
    )


def set_branches(session, tenant, branches):
    """Replace a session's branch set, keeping is_school_wide in step.

    ``branches`` empty means the whole school. The flag and the rows are written
    together and only here, because a partial unique constraint reads the flag
    and the two disagreeing would silently disable a guard.
    """
    SessionBranch.all_objects.filter(session=session).delete()
    SessionBranch.all_objects.bulk_create([
        SessionBranch(
            tenant=tenant, session=session, branch=b,
            session_status=session.status,
        )
        for b in branches
    ])
    school_wide = not branches
    if session.is_school_wide != school_wide:
        session.is_school_wide = school_wide
        session.save(update_fields=["is_school_wide", "updated_at"])


def _sync_link_status(session):
    SessionBranch.all_objects.filter(session=session).update(
        session_status=session.status,
    )


@transaction.atomic
def activate_session(session, tenant, actor=None):
    """Promote *session*, displacing whatever covered the branches it claims.

    Returns the list of sessions this narrowed or archived, so the caller can
    tell the school what moved.
    """
    if session.status == SessionStatus.ACTIVE:
        return []                       # 200 no-op; activated_at is not rewritten

    # Lock every session of the tenant, and the links, before deciding
    # anything: a rule evaluated outside the lock can be true when it is read
    # and false when it commits.
    locked = list(
        AcademicSession.all_objects
        .select_for_update()
        .filter(tenant=tenant)
        .order_by("pk")
    )
    session = next(s for s in locked if s.pk == session.pk)

    # An archived year comes back with its terms. Without this, FR-002 rule 4
    # below would refuse every archived session by definition - archiving a
    # year is what archived its terms - and the route would be dead on arrival.
    if session.status == SessionStatus.ARCHIVED:
        AcademicTerm.all_objects.filter(session=session).update(archived_at=None)
        session.archived_at = None

    archived_terms = list(
        AcademicTerm.all_objects
        .filter(session=session, archived_at__isnull=False)
        .values_list("id", flat=True)
    )
    if archived_terms:
        # Defence in depth since version 2.5: no route can produce this state,
        # but a database edit or a route added later can, and an ACTIVE session
        # holding a retired term tells a school it is in a term it archived.
        raise SessionHasArchivedTerm(
            "This session cannot be activated while it holds an archived term.",
            terms=archived_terms,
        )

    claimed = covered_branch_ids(session, tenant)
    displaced = []

    for other in locked:
        if other.pk == session.pk or other.status != SessionStatus.ACTIVE:
            continue
        theirs = covered_branch_ids(other, tenant)
        remaining = theirs - claimed
        if remaining == theirs:
            continue                    # nothing of theirs was claimed
        if remaining:
            _narrow(other, tenant, remaining, theirs - remaining, actor)
        else:
            _archive(other, tenant, actor, reason="displaced")
        displaced.append(other)

    session.status = SessionStatus.ACTIVE
    session.activated_at = session.activated_at or timezone.now()
    session.save(update_fields=[
        "status", "activated_at", "archived_at", "updated_at",
    ])
    _sync_link_status(session)

    emit_audit_event(
        module_key=AuditModuleKey.ACADEMICS,
        action_type=AuditActionType.ACADEMIC_SESSION_ACTIVATED,
        entity_type="AcademicSession",
        entity_id=str(session.pk),
        entity_label=session.name,
        tenant=tenant,
        actor_user=actor,
        summary=f"{session.name} is now the active session.",
        metadata={"displaced": [s.pk for s in displaced]},
    )
    return displaced


def _narrow(session, tenant, remaining_ids, lost_ids, actor):
    """Take branches away from a live session without ending it."""
    from vs_tenants.models import Branch

    keep = list(Branch.all_objects.filter(tenant=tenant, id__in=remaining_ids))
    set_branches(session, tenant, keep)
    _sync_link_status(session)
    emit_audit_event(
        module_key=AuditModuleKey.ACADEMICS,
        action_type=AuditActionType.ACADEMIC_SESSION_NARROWED,
        entity_type="AcademicSession",
        entity_id=str(session.pk),
        entity_label=session.name,
        tenant=tenant,
        actor_user=actor,
        summary=f"{session.name} no longer covers every branch it did.",
        metadata={"lost": sorted(lost_ids), "kept": sorted(remaining_ids)},
    )


@transaction.atomic
def archive_session(session, tenant, actor=None):
    """Archive a year and every term in it, in one transaction."""
    return _archive(session, tenant, actor)


def _archive(session, tenant, actor, reason="requested"):
    now = timezone.now()
    session.status = SessionStatus.ARCHIVED
    session.archived_at = session.archived_at or now
    session.save(update_fields=["status", "archived_at", "updated_at"])
    _sync_link_status(session)

    terms = list(AcademicTerm.all_objects.filter(session=session))
    AcademicTerm.all_objects.filter(session=session).update(archived_at=now)

    emit_audit_event(
        module_key=AuditModuleKey.ACADEMICS,
        action_type=AuditActionType.ACADEMIC_SESSION_ARCHIVED,
        entity_type="AcademicSession",
        entity_id=str(session.pk),
        entity_label=session.name,
        tenant=tenant,
        actor_user=actor,
        summary=f"{session.name} archived.",
        metadata={"reason": reason, "terms": [t.pk for t in terms]},
    )
    for term in terms:
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.ACADEMIC_TERM_ARCHIVED,
            entity_type="AcademicTerm",
            entity_id=str(term.pk),
            entity_label=term.name,
            tenant=tenant,
            actor_user=actor,
            summary=f"{term.name} archived with {session.name}.",
        )
    return session


# ── Terms ─────────────────────────────────────────────────────────────────

def validate_terms(session, terms):
    """The four rules a set of terms has to satisfy, checked together.

    *terms* is a list of dicts carrying ``name``, ``order_index``,
    ``start_date`` and ``end_date``. Checked as a set rather than one at a time
    because three of the four rules are about a term's relationship to its
    siblings, and a per-row validator cannot see them.
    """
    for t in terms:
        if t["end_date"] <= t["start_date"]:
            raise TermOutsideSession(
                f"{t['name']} ends on or before it starts.",
                term=t["name"],
            )
        if t["start_date"] < session.start_date or t["end_date"] > session.end_date:
            raise TermOutsideSession(
                f"{t['name']} falls outside the session dates.",
                term=t["name"],
            )

    by_date = sorted(terms, key=lambda t: t["start_date"])
    for earlier, later in zip(by_date, by_date[1:]):
        if later["start_date"] <= earlier["end_date"]:
            raise TermDatesOverlap(
                f"{later['name']} overlaps {earlier['name']}.",
                conflicts_with=earlier["name"],
            )

    # Non-overlap does not imply correct ordering: two terms can be disjoint
    # and still numbered backwards, and every consumer reads terms in
    # order_index order, so a year numbered backwards renders out of sequence
    # with no error anywhere.
    by_index = sorted(terms, key=lambda t: t["order_index"])
    if [t["name"] for t in by_index] != [t["name"] for t in by_date]:
        raise TermOrderConflict(
            "The order these terms are numbered in disagrees with their dates.",
            by_number=[t["name"] for t in by_index],
            by_date=[t["name"] for t in by_date],
        )


def assert_term_deletable(term):
    """A term may be deleted only while its year is still a draft.

    Keyed on the session's status and on nothing else, deliberately, so the
    correction for a mistyped term stays available for the whole of the draft
    window and cannot be shut by the state it exists to correct.
    """
    assert_writable(term.session)
    if term.session.status != SessionStatus.DRAFT:
        raise TermSessionNotDraft(
            f"{term.name} cannot be deleted because {term.session.name} has "
            f"already started. Edit its dates instead.",
            session=term.session.name,
            status=term.session.status,
        )
