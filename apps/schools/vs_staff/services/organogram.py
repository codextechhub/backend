"""The school's org chart, and the questions other code asks of it.

The same service the platform's chart has in ``vs_user.services.organogram``,
scoped to one tenant and aware of branches. It owns appointing somebody to a
post and ending it, reading who holds a post, walking the reporting line,
building the tree, finding the open seats, and the three climbs the workflow
engine uses to find an approver relative to whoever raised a document.

**Every query names its tenant.** The workflow engine asks from outside any
request, where there is no ambient tenant to lean on, and an unscoped climb
would be a way for one school's document to reach another school's staff.

**Holding a post is narrower than being appointed to it.** An appointment is
open until it is ended; a holder is somebody in an open appointment who has
accepted their invitation and still works here. The rule is :func:`holding_q`,
written once, because the chart, the vacancies, the summary and a person's line
manager all have to agree on who is in a seat.

**A suspended person still holds their post.** A teacher suspended for a week
is still the Annex's class teacher, and a chart that emptied the seat for that
week would read as a post to fill. The chart draws them in it, marked
suspended. What they cannot do is approve anything, since their account is
closed and they cannot sign in: the approver climbs ask
:func:`approving_q`, which also requires an active account, so a suspended
manager is passed over rather than handed a document nobody can decide.
"""
from __future__ import annotations

from typing import List, Optional

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from vs_config.clock import tenant_today

from ..constants import NON_HOLDING_STATUSES, OFF_ROLL_STATUSES, OrgUnitKind
from ..exceptions import NotEligibleForPost


def holding_q(prefix: str = "") -> Q:
    """Open appointments whose person is in the seat today. See the module docstring."""
    return (
        Q(**{f"{prefix}end_date__isnull": True})
        & ~Q(**{f"{prefix}staff__employment_status__in": tuple(NON_HOLDING_STATUSES)})
    )


def approving_q(prefix: str = "") -> Q:
    """Holders who can act on a document: :func:`holding_q` with an active account."""
    return holding_q(prefix) & Q(**{f"{prefix}staff__user__is_active": True})


def holders_prefetch(to_attr: str = "current_appointments") -> Prefetch:
    """A post's holders in one query for a whole page, primary appointments first."""
    from ..models import StaffPositionAssignment

    return Prefetch(
        "appointments",
        queryset=(
            StaffPositionAssignment.all_objects.filter(holding_q())
            .select_related("staff__user")
            .order_by("-is_primary", "id")
        ),
        to_attr=to_attr,
    )


def holders_of(position) -> list:
    """The staff records holding *position*, primary first.

    Reads the prefetch where the caller made one, and asks otherwise.
    """
    from ..models import StaffPositionAssignment

    appointments = getattr(position, "current_appointments", None)
    if appointments is None:
        appointments = (
            StaffPositionAssignment.all_objects.filter(position_id=position.pk)
            .filter(holding_q())
            .select_related("staff__user")
            .order_by("-is_primary", "id")
        )
    return [appointment.staff for appointment in appointments]


class StaffOrganogramService:
    """Appointments, the reporting line, the tree, and the approver climbs."""

    # ── Appointments ──────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def assign_position(staff, position, *, is_primary=True, is_acting=False,
                        start_date=None):
        """Appoint *staff* to *position* from *start_date*, today by default.

        A primary appointment first ends the person's current primary one on
        the day the new one starts, so "one current primary" holds without a
        constraint MariaDB cannot express. The person's row is locked for the
        length of the transaction, so two appointments made at once cannot both
        find no primary and both write one.

        Refused, as :class:`~..exceptions.NotEligibleForPost`, for a post no
        longer in use, for somebody who no longer works here, for a post in a
        branch the person is not posted to, and for a post they already hold.
        An invited person may be appointed: the seat is reserved for them and
        reads vacant until they accept.
        """
        from ..models import StaffPositionAssignment, StaffProfile

        StaffProfile.all_objects.select_for_update().filter(pk=staff.pk).first()
        if staff.tenant_id != position.tenant_id:
            raise NotEligibleForPost("That post belongs to a different school.")
        if not position.is_active:
            raise NotEligibleForPost(
                f"{position.title} is no longer in use, so nobody can be appointed "
                f"to it. Reactivate it first.",
            )
        if staff.employment_status in OFF_ROLL_STATUSES:
            raise NotEligibleForPost(
                "They no longer work here, so they cannot be appointed to a post.",
            )

        start = start_date or tenant_today(staff.tenant)
        if is_primary:
            current = list(
                StaffPositionAssignment.all_objects.filter(
                    staff=staff, is_primary=True, end_date__isnull=True,
                ).exclude(position=position)
            )
            later = [row for row in current if row.start_date > start]
            if later:
                raise NotEligibleForPost(
                    f"Their current primary post started on {later[0].start_date}, "
                    f"after {start}. Start the new appointment on or after that day.",
                )
            for row in current:
                row.end_date = start
                row.save(update_fields=["end_date", "updated_at"])

        assignment = StaffPositionAssignment(
            tenant_id=staff.tenant_id, staff=staff, position=position,
            is_primary=is_primary, is_acting=is_acting, start_date=start,
        )
        try:
            assignment.clean()
        except DjangoValidationError as error:
            raise NotEligibleForPost(_first_message(error))
        assignment.save()
        return assignment

    @staticmethod
    @transaction.atomic
    def end_assignment(assignment, end_date=None):
        """End an open appointment, today by default. An ended one is left alone."""
        if assignment.end_date is not None:
            return assignment
        end = end_date or tenant_today(assignment.tenant)
        if end < assignment.start_date:
            raise NotEligibleForPost(
                f"This appointment started on {assignment.start_date}, so it cannot "
                f"end before then.",
            )
        assignment.end_date = end
        assignment.save(update_fields=["end_date", "updated_at"])
        return assignment

    @staticmethod
    def close_for_exit(staff, end_date=None) -> int:
        """End every open appointment of somebody who has left, on their exit date.

        Called by the two transitions that end employment, inside their own
        transaction, so a person is never gone from the school and still
        sitting in a post. The post stays on the chart, vacant, with its reports
        still under it for whoever is appointed next.

        An appointment that started after the exit date ends on the day it
        started, since an appointment cannot end before it begins.
        """
        from ..models import StaffPositionAssignment

        end = end_date or tenant_today(staff.tenant)
        closed = 0
        for row in StaffPositionAssignment.all_objects.filter(
            staff=staff, end_date__isnull=True,
        ):
            row.end_date = max(end, row.start_date)
            row.save(update_fields=["end_date", "updated_at"])
            closed += 1
        return closed

    # ── Reading a person's place on the chart ─────────────────────────────

    @staticmethod
    def primary_assignment_for(staff, on=None):
        """The person's primary appointment, current or as it stood on *on*.

        As at a day, an appointment counts from its start date and up to, not
        including, its end date: the day a new primary starts is the day the
        old one ends, and the person held one post that day, not two.
        """
        from ..models import StaffPositionAssignment

        if staff is None:
            return None
        rows = StaffPositionAssignment.all_objects.filter(
            tenant_id=staff.tenant_id, staff=staff, is_primary=True,
        )
        if on is None:
            rows = rows.filter(end_date__isnull=True)
        else:
            rows = rows.filter(start_date__lte=on).filter(
                Q(end_date__isnull=True) | Q(end_date__gt=on),
            )
        return (
            rows.select_related(
                "position__org_node", "position__reports_to", "position__org_node__branch",
            )
            .order_by("-start_date", "-id")
            .first()
        )

    @staticmethod
    def primary_position_for(staff):
        """The person's current primary post, or None."""
        assignment = StaffOrganogramService.primary_assignment_for(staff)
        return assignment.position if assignment else None

    @staticmethod
    def manager_chain(staff) -> list:
        """The posts above the person's primary post, nearest first.

        The line is read in one query as a map of post to manager post, then
        walked, so a chain ten posts tall costs two queries rather than eleven.
        A loop the model forbids is still stopped rather than followed.
        """
        from ..models import StaffPosition

        position = StaffOrganogramService.primary_position_for(staff)
        if position is None:
            return []
        rows = {
            row.pk: row for row in
            StaffPosition.all_objects.filter(tenant_id=staff.tenant_id)
            .only("id", "title", "code", "reports_to_id", "org_node_id", "tenant_id")
        }
        chain = []
        seen = {position.pk}
        manager_id = position.reports_to_id
        while manager_id is not None and manager_id not in seen and manager_id in rows:
            manager = rows[manager_id]
            chain.append(manager)
            seen.add(manager_id)
            manager_id = manager.reports_to_id
        return chain

    # ── The chart ─────────────────────────────────────────────────────────

    @staticmethod
    def build_tree(tenant, root=None) -> list:
        """The active posts as a nested tree, from the top or from *root*.

        One query for the posts and one for their holders, whatever the size of
        the school. A post whose manager post is inactive is drawn as a root
        rather than dropped, or retiring one post would take everybody under it
        off the chart.
        """
        from ..models import StaffPosition

        positions = list(
            StaffPosition.all_objects.filter(tenant=tenant, is_active=True)
            .select_related("org_node__branch")
            .prefetch_related(holders_prefetch())
            .order_by("title", "id")
        )
        active_ids = {position.pk for position in positions}
        children: dict = {}
        for position in positions:
            parent = position.reports_to_id if position.reports_to_id in active_ids else None
            children.setdefault(parent, []).append(position)

        def node_for(position, seen):
            holders = holders_of(position)
            seen = seen | {position.pk}
            return {
                "id": position.pk,
                "title": position.title,
                "code": position.code,
                "org_node": position.org_node,
                "branch": position.org_node.branch if position.org_node_id else None,
                "holders": holders,
                "is_vacant": not holders,
                "direct_reports": [
                    node_for(child, seen)
                    for child in children.get(position.pk, [])
                    if child.pk not in seen
                ],
            }

        if root is not None:
            loaded = next((p for p in positions if p.pk == root.pk), None)
            if loaded is None:
                loaded = (
                    StaffPosition.all_objects.filter(tenant=tenant, pk=root.pk)
                    .select_related("org_node__branch")
                    .prefetch_related(holders_prefetch())
                    .first()
                )
            return [node_for(loaded, frozenset())] if loaded is not None else []
        return [node_for(position, frozenset()) for position in children.get(None, [])]

    @staticmethod
    def vacancies(tenant) -> list:
        """Active posts with at least one open seat, in title order."""
        from ..models import StaffPosition

        positions = (
            StaffPosition.all_objects.filter(tenant=tenant, is_active=True)
            .select_related("org_node__branch", "reports_to__org_node")
            .prefetch_related(holders_prefetch())
            .order_by("title", "id")
        )
        return [
            position for position in positions
            if position.headcount > len(holders_of(position))
        ]

    @staticmethod
    def summary(tenant, user=None) -> dict:
        """The establishment in nine numbers, for the header above the chart.

        Seats are counted post by post, so a post holding more people than its
        headcount fills its own seats and never another post's. ``acting``
        counts people covering a post rather than holding it. ``on_leave`` reads
        leave the same way the directory does, and counts only people who are
        otherwise active.

        **The numbers follow the reader's branches; the chart does not.** Every
        member of staff reads the whole school's chart, but who is on leave or
        suspended is a branch administrator's business for their own branch,
        so given a *user* the people, units and posts counted are the ones the
        staff directory would show them: their branches' and the school-wide
        ones. A whole-school reader, or no *user*, counts everything.
        """
        from django.db.models import Count

        from schools.vs_academics.services.scoping import scope_to_visible_branches

        from ..constants import EmploymentStatus
        from ..models import StaffOrgNode, StaffPosition, StaffProfile
        from .leave import on_leave_expression
        from .scoping import scope_staff

        def narrowed(queryset, field):
            if user is None:
                return queryset
            return scope_to_visible_branches(queryset, user, tenant, field=field)

        staff = StaffProfile.all_objects.filter(tenant=tenant)
        if user is not None:
            staff = scope_staff(staff, user, tenant, include_self=False)
        people = (
            staff
            .annotate(away=on_leave_expression())
            .aggregate(
                active=Count("pk", filter=Q(employment_status=EmploymentStatus.ACTIVE)),
                on_leave=Count(
                    "pk", filter=Q(employment_status=EmploymentStatus.ACTIVE, away=True),
                ),
                suspended=Count(
                    "pk", filter=Q(employment_status=EmploymentStatus.SUSPENDED),
                ),
            )
        )
        departments = narrowed(
            StaffOrgNode.all_objects.filter(
                tenant=tenant, is_active=True, kind=OrgUnitKind.DEPARTMENT,
            ),
            "branch",
        ).count()
        positions = list(
            narrowed(
                StaffPosition.all_objects.filter(tenant=tenant, is_active=True),
                "org_node__branch",
            )
            .only("id", "headcount")
            .prefetch_related(holders_prefetch())
        )
        total = sum(position.headcount for position in positions)
        filled = sum(
            min(position.headcount, len(position.current_appointments))
            for position in positions
        )
        acting = sum(
            1 for position in positions
            for appointment in position.current_appointments
            if appointment.is_acting
        )
        return {
            "active_staff": people["active"],
            "departments": departments,
            "positions": len(positions),
            "total_seats": total,
            "filled_seats": filled,
            "vacant_seats": total - filled,
            "acting": acting,
            "on_leave": people["on_leave"],
            "suspended": people["suspended"],
        }

    # ── Workflow: organogram-based approvers ──────────────────────────────
    #
    # The workflow engine never imports this module. ``VsStaffConfig.ready``
    # registers this class as a school tenant's organogram, and the engine calls
    # the methods below by name, always passing the tenant to look inside.

    @staticmethod
    def _staff_for(user, tenant):
        from ..models import StaffProfile

        if user is None or tenant is None:
            return None
        return StaffProfile.all_objects.filter(
            tenant=tenant, user_id=user.pk,
        ).select_related("user").first()

    @staticmethod
    def _holder_users(position_id, tenant, exclude_user=None) -> list:
        """The people in *tenant* who hold the post and can act on a document.

        The tenant is part of the lookup, not assumed from the id: a post id
        from another school, however it arrived, reaches nobody.
        """
        from ..models import StaffPositionAssignment

        if tenant is None:
            return []
        rows = (
            StaffPositionAssignment.all_objects.filter(
                position_id=position_id, tenant=tenant, position__tenant=tenant,
            )
            .filter(approving_q())
            .select_related("staff__user")
            .order_by("-is_primary", "id")
        )
        users = [row.staff.user for row in rows]
        if exclude_user is not None:
            users = [user for user in users if user.pk != exclude_user.pk]
        return users

    @staticmethod
    def resolve_direct_manager(user, tenant) -> List:
        """DIRECT_MANAGER: whoever holds the post the requester's primary post reports to."""
        staff = StaffOrganogramService._staff_for(user, tenant)
        position = StaffOrganogramService.primary_position_for(staff)
        if position is None or position.reports_to_id is None:
            return []
        return StaffOrganogramService._holder_users(position.reports_to_id, tenant, user)

    @staticmethod
    def resolve_n_levels_up(user, levels, tenant) -> List:
        """N_LEVELS_UP: whoever holds the post *levels* above, or the top of the line."""
        levels = max(int(levels or 1), 1)
        staff = StaffOrganogramService._staff_for(user, tenant)
        chain = StaffOrganogramService.manager_chain(staff)
        if not chain:
            return []
        target = chain[min(levels, len(chain)) - 1]
        return StaffOrganogramService._holder_users(target.pk, tenant, user)

    @staticmethod
    def resolve_department_head(user, tenant) -> List:
        """DEPARTMENT_HEAD: the nearest unit above the requester with a filled head post.

        Walks from the unit the requester's primary post sits in up through its
        parents, and stops at the first whose head post is held by somebody
        other than the requester, so the head of a team who files leave reaches
        the head of the department above rather than themselves.
        """
        staff = StaffOrganogramService._staff_for(user, tenant)
        position = StaffOrganogramService.primary_position_for(staff)
        if position is None:
            return []
        unit = position.org_node
        seen = set()
        while unit is not None and unit.pk not in seen:
            seen.add(unit.pk)
            if unit.head_position_id is not None:
                resolved = StaffOrganogramService._holder_users(
                    unit.head_position_id, tenant, user,
                )
                if resolved:
                    return resolved
            unit = unit.parent
        return []

    @staticmethod
    def find_position(code, tenant):
        """``(id, code, title)`` of the school's active post with *code*, or None.

        How a stage or an approver group names a post: the code a person types
        is looked up here, and the id is what the engine keeps. Codes are stored
        upper-case, so the match ignores case.
        """
        from ..models import StaffPosition

        code = (code or "").strip()
        if tenant is None or not code:
            return None
        return (
            StaffPosition.all_objects.filter(
                tenant=tenant, code__iexact=code, is_active=True,
            )
            .values_list("id", "code", "title")
            .first()
        )

    @staticmethod
    def describe_positions(ids, tenant) -> dict:
        """``{id: (code, title)}`` for the school's posts among *ids*, in one query.

        Inactive posts are described too: a stage or a group still naming one
        has to be able to say which it is.
        """
        from ..models import StaffPosition

        ids = [pk for pk in ids if pk is not None]
        if tenant is None or not ids:
            return {}
        return {
            pk: (code, title)
            for pk, code, title in StaffPosition.all_objects.filter(
                tenant=tenant, pk__in=ids,
            ).values_list("id", "code", "title")
        }

    @staticmethod
    def describe_position(position_id, tenant):
        """``(code, title)`` of one of the school's posts, or None."""
        return StaffOrganogramService.describe_positions([position_id], tenant).get(position_id)

    @staticmethod
    def resolve_position_holders(position_id, tenant, exclude_user=None) -> List:
        """SPECIFIC_POSITION and POSITION members: whoever holds one named post.

        Holders with an active account only (:func:`approving_q`), so a
        suspended holder keeps the seat on the chart and is passed over here.
        """
        return StaffOrganogramService._holder_users(position_id, tenant, exclude_user)


def _first_message(error: DjangoValidationError) -> str:
    """The first human sentence out of a model validation error."""
    if hasattr(error, "message_dict"):
        for messages in error.message_dict.values():
            if messages:
                return messages[0]
    messages = getattr(error, "messages", None) or []
    return messages[0] if messages else NotEligibleForPost.default_message


def primary_line_for(staff, on=None) -> Optional[dict]:
    """The ``organogram`` block of one person's record, or None.

    Their primary post, its unit, the holder of the post it reports to, and
    whether they are acting. Built for a single record: two or three queries,
    which is why the directory's list rows do not carry it.
    """
    from ..models import StaffPositionAssignment

    assignment = StaffOrganogramService.primary_assignment_for(staff, on=on)
    if assignment is None:
        return None
    position = assignment.position
    manager = None
    if position.reports_to_id is not None:
        rows = StaffPositionAssignment.all_objects.filter(
            position_id=position.reports_to_id,
        ).select_related("staff__user").order_by("-is_primary", "id")
        if on is None:
            rows = rows.filter(holding_q())
        else:
            rows = rows.filter(start_date__lte=on).filter(
                Q(end_date__isnull=True) | Q(end_date__gt=on),
            )
        row = rows.exclude(staff_id=staff.pk).first()
        manager = row.staff if row is not None else None
    return {
        "position": position,
        "org_node": position.org_node,
        "line_manager": manager,
        "is_acting": assignment.is_acting,
    }
