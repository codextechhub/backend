"""The numbers a directory draws its header from.

Returned beside the page rather than from a second endpoint, because a directory
that needs two calls to draw its header shows the header late.

Six counts, and they are not all the same kind of thing, which is why each is
labelled rather than pooled. Total and "currently employed" differ the moment
somebody resigns. The employment breakdown draws the bar. **Locked accounts is
an account-status count sitting beside employment ones** and must be labelled as
such, or a school reads a security lockout as a suspension. And the side
breakdown is by branch at a school with several and **by role at a school with
one**, because a column repeating the same value on every row is noise and the
dimension should recede rather than be padded.

FRD M12 v2.1, FR-002.
"""
from __future__ import annotations

from django.db.models import Count, Q

from ..constants import EmploymentStatus
from .scoping import branch_dimension_applies


def counts(queryset, tenant):
    """Everything the directory header shows, from three queries.

    ``queryset`` is the caller's already-scoped staff queryset, so every figure
    here counts the same people the list does. A header that counted the school
    and a list that showed a branch would be a header nobody could reconcile.

    **It arrives annotated and ordered for display, and neither survives being
    counted.** Every figure below therefore goes through :func:`_countable`,
    which strips the ordering, and counts ``pk`` distinctly. Both halves matter
    and they fail in opposite directions: a ``.values(...).annotate(...)`` over
    an ordered queryset adds the ordering columns to the GROUP BY, so each row
    becomes its own group and a status held by seven people reports one; and the
    ``teaching_load`` join multiplies a row once per assignment, so a school of
    twelve reports fifteen. A header that overstates and understates at the same
    time is worse than one that is merely wrong, because the two figures beside
    each other look like a rounding difference rather than a defect.
    """
    from vs_user.models import User

    countable = _countable(queryset)
    by_status = {
        row["employment_status"]: row["n"]
        for row in countable.values("employment_status").annotate(
            n=Count("pk", distinct=True),
        )
    }
    total = sum(by_status.values())
    on_roll = total - sum(
        by_status.get(status, 0)
        for status in (EmploymentStatus.RESIGNED, EmploymentStatus.TERMINATED)
    )

    aggregate = countable.aggregate(
        with_teaching=Count(
            "pk", filter=Q(teaching_assignments__isnull=False), distinct=True,
        ),
        locked=Count(
            "pk", filter=Q(user__status=User.Status.LOCKED), distinct=True,
        ),
    )

    payload = {
        "total": total,
        "currently_employed": on_roll,
        "by_employment_status": [
            {
                "value": status,
                "label": label,
                "count": by_status.get(status, 0),
            }
            for status, label in EmploymentStatus.choices
            if by_status.get(status)
        ],
        "with_teaching_duties": aggregate["with_teaching"] or 0,
        # An ACCOUNT count, deliberately beside the employment ones and named so
        # nobody reads a lockout as an employment state.
        "locked_accounts": aggregate["locked"] or 0,
    }
    payload.update(_side_breakdown(countable, tenant))
    return payload


def _countable(queryset):
    """The same people, with the display ordering removed.

    One place, because the ordering is inherited from the list queryset and
    every aggregate below would otherwise have to remember to drop it. Django
    folds ``order_by`` columns into the GROUP BY of a ``.values().annotate()``,
    so an ordering by ``created_at`` and ``id`` groups by the row itself.

    The ``teaching_load`` annotation is left alone: dropping it would mean
    rebuilding the queryset and losing the caller's scoping with it, and
    counting ``pk`` distinctly answers the join it introduces.
    """
    return queryset.order_by()


def _side_breakdown(queryset, tenant):
    """By branch where a school has several, by role where it has one.

    At a single-branch school the branch panel would repeat one value on every
    row, so the dimension is absent rather than disabled and the role
    distribution takes the space. Nothing about the data changes with it: the
    panel switches back the day a second branch opens, without a row being
    rewritten.
    """
    if branch_dimension_applies(tenant):
        rows = (
            queryset.values("branch_id", "branch__name")
            .annotate(n=Count("pk", distinct=True))
            .order_by("branch__name")
        )
        return {
            "breakdown_by": "branch",
            "breakdown": [
                {
                    "value": row["branch_id"],
                    # A null posting is "school-wide", which is a real answer
                    # and never blank or "Not set".
                    "label": row["branch__name"] or "School-wide",
                    "count": row["n"],
                }
                for row in rows
            ],
        }

    rows = (
        queryset.values("user__tenant_role_assignments__role__name")
        .filter(user__tenant_role_assignments__assignment_status="ACTIVE")
        .annotate(n=Count("pk", distinct=True))
        .order_by("user__tenant_role_assignments__role__name")
    )
    return {
        "breakdown_by": "role",
        "breakdown": [
            {
                "value": row["user__tenant_role_assignments__role__name"],
                "label": row["user__tenant_role_assignments__role__name"] or "No role",
                "count": row["n"],
            }
            for row in rows
        ],
    }
