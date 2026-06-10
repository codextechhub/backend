# services/organogram.py
# Business logic for the CX-staff organogram (org chart).
#
# OrganogramService owns:
#   - Assigning a user to a position (with primary-seat handling + history)
#   - Closing / ending an assignment
#   - Resolving a user's manager chain along the solid reporting line
#   - Building the position tree
#   - Listing vacancies
#   - Resolving organogram-based approvers for the workflow engine
#     (the four climb modes: direct manager, N levels up, department head,
#      specific position)

from __future__ import annotations

from typing import List, Optional

from django.db import transaction
from django.utils import timezone

from ..models import (
    User,
    OrgNode,
    Position,
    PositionAssignment,
    PlatformStaffProfile,
)


class OrganogramService:

    # ── Assignment ─────────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def assign_position(
        user: User,
        position: Position,
        *,
        is_primary: bool = True,
        is_acting: bool = False,
        start_date=None,
        assigned_by: User = None,
    ) -> PositionAssignment:
        """
        Assigns `user` to `position`, creating a new effective-dated tenure.

        If `is_primary`, any existing current primary assignment for the user is
        closed first (end_date = today) so the "one current primary" invariant
        holds — MariaDB cannot express this as a conditional unique constraint.

        On a primary assignment the user's PlatformStaffProfile.position is
        synced to this position, so department, line manager, and the whole
        reporting chain settle immediately off that one seat.
        """
        if user.user_type != User.UserType.CX_STAFF:
            raise ValueError({
                'error_code': 'NOT_CX_STAFF',
                'message': 'Only CX Staff can be assigned to a position.',
            })

        start = start_date or timezone.localdate()

        if is_primary:
            # Close the user's current primary tenure, if any.
            (
                PositionAssignment.objects
                .filter(user=user, is_primary=True, end_date__isnull=True)
                .update(end_date=start)
            )

        assignment = PositionAssignment.objects.create(
            user=user,
            position=position,
            is_primary=is_primary,
            is_acting=is_acting,
            start_date=start,
        )

        if is_primary:
            OrganogramService._sync_profile_position(user, position)

        return assignment

    @staticmethod
    @transaction.atomic
    def end_assignment(assignment: PositionAssignment, end_date=None) -> PositionAssignment:
        """Closes an open assignment. No-op if already ended."""
        if assignment.end_date is not None:
            return assignment
        assignment.end_date = end_date or timezone.localdate()
        assignment.save(update_fields=['end_date', 'updated_at'])

        if assignment.is_primary:
            # Position cache no longer reflects a live primary seat.
            OrganogramService._sync_profile_position(assignment.user, None)
        return assignment

    @staticmethod
    def _sync_profile_position(user: User, position: Optional[Position]) -> None:
        profile = (
            PlatformStaffProfile.objects.filter(user=user).first()
        )
        if profile is None:
            return
        new_position_id = position.pk if position else None
        if profile.position_id != new_position_id:
            profile.position_id = new_position_id
            profile.save(update_fields=['position', 'updated_at'])

    # ── Manager-chain resolution ───────────────────────────────────────────────

    @staticmethod
    def primary_position_for(user: User) -> Optional[Position]:
        """The user's current primary Position, or None."""
        assignment = (
            PositionAssignment.objects
            .filter(user=user, is_primary=True, end_date__isnull=True)
            .select_related('position', 'position__org_node')
            .first()
        )
        return assignment.position if assignment else None

    @staticmethod
    def manager_chain(user: User) -> List[Position]:
        """
        Returns the chain of manager *positions* above the user's primary
        position, nearest first, walking Position.reports_to to the top.
        """
        chain: List[Position] = []
        position = OrganogramService.primary_position_for(user)
        if position is None:
            return chain
        seen = {position.pk}
        node = position.reports_to
        while node is not None and node.pk not in seen:
            chain.append(node)
            seen.add(node.pk)
            node = node.reports_to
        return chain

    # ── Tree builder ───────────────────────────────────────────────────────────

    @staticmethod
    def build_tree(root: Optional[Position] = None) -> list:
        """
        Builds the position tree as nested dicts suitable for
        OrgTreeNodeSerializer. If `root` is given, builds the subtree under
        it; otherwise returns all top-level positions (reports_to IS NULL).
        """
        positions = list(
            Position.objects
            .filter(is_active=True)
            .select_related('org_node')
            .prefetch_related('assignments__user')
        )
        children_by_parent: dict = {}
        roots: list = []
        for pos in positions:
            children_by_parent.setdefault(pos.reports_to_id, []).append(pos)

        def node_for(pos: Position) -> dict:
            return {
                'id': pos.id,
                'title': pos.title,
                'code': pos.code,
                'org_node': pos.org_node,
                'holders': pos.current_holders,
                'is_vacant': pos.is_vacant,
                'direct_reports': [
                    node_for(child)
                    for child in children_by_parent.get(pos.id, [])
                ],
            }

        if root is not None:
            return [node_for(root)]

        for pos in children_by_parent.get(None, []):
            roots.append(node_for(pos))
        return roots

    # ── Vacancies ──────────────────────────────────────────────────────────────

    @staticmethod
    def vacancies():
        """Active positions with at least one open seat."""
        return [
            pos for pos in (
                Position.objects.filter(is_active=True).select_related('org_node')
            )
            if pos.open_seats > 0
        ]

    # ── Workflow integration: organogram-based approver resolution ─────────────

    @staticmethod
    def resolve_direct_manager(user: User) -> List[User]:
        """Climb mode: DIRECT_MANAGER — holder of the parent (reports_to) seat."""
        position = OrganogramService.primary_position_for(user)
        if position is None or position.reports_to_id is None:
            return []
        holders = position.reports_to.current_holders
        return [u for u in holders if u and u.pk != user.pk]

    @staticmethod
    def resolve_n_levels_up(user: User, levels: int) -> List[User]:
        """Climb mode: N_LEVELS_UP — holder(s) of the seat `levels` up the chain."""
        levels = max(int(levels or 1), 1)
        chain = OrganogramService.manager_chain(user)
        if not chain:
            return []
        # Clamp to the top of the chain if N exceeds its height.
        target = chain[min(levels, len(chain)) - 1]
        return [u for u in target.current_holders if u and u.pk != user.pk]

    @staticmethod
    def resolve_department_head(user: User) -> List[User]:
        """
        Climb mode: DEPARTMENT_HEAD — holder of the head_position of the org node
        the user sits in, walking UP the org tree (Team → Department → Division)
        until a node with a filled head seat is found.
        """
        position = OrganogramService.primary_position_for(user)
        if position is None:
            return []
        node = position.org_node
        while node is not None:
            if node.head_position_id is not None:
                holders = node.head_position.current_holders
                resolved = [u for u in holders if u and u.pk != user.pk]
                if resolved:
                    return resolved
            node = node.parent
        return []

    @staticmethod
    def resolve_specific_position(position: Position, exclude_user: User = None) -> List[User]:
        """Climb mode: SPECIFIC_POSITION — current holders of an explicit seat."""
        if position is None:
            return []
        holders = position.current_holders
        if exclude_user is not None:
            holders = [u for u in holders if u and u.pk != exclude_user.pk]
        return holders
