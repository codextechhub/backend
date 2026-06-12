"""Organogram-derived hierarchy helpers for vs_todo.

The ToDo design treats the org as a tree: a person has a manager, a manager has
reports, and "your area" is you plus everyone beneath you. None of that is
stored on the task — it is read live from the existing CX organogram in vs_user
(Position.reports_to / PositionAssignment), so the ToDo tree always tracks the
current org structure.

This module is the single place that translates organogram *positions* into the
*people* the design works with (descendants, area, the chain to the root,
assign-ability). Everything is computed from one pass over the active position
tree to avoid per-node queries.

Mirrors the design's tree helpers: childrenOf / descendantsOf / areaIds /
canAssign / chainTo.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from vs_user.models import Position, PositionAssignment, User


class TodoHierarchy:
    # ── Position lookups ──────────────────────────────────────────────────────

    @staticmethod
    def primary_position(user: User) -> Optional[Position]:
        """The user's current primary Position, or None."""
        assignment = (
            PositionAssignment.objects
            .filter(user=user, is_primary=True, end_date__isnull=True)
            .select_related("position", "position__org_node")
            .first()
        )
        return assignment.position if assignment else None

    @staticmethod
    def _children_index() -> Dict[Optional[int], List[Position]]:
        """One pass over active positions → {reports_to_id: [child positions]}.

        Built per call so it always reflects the current tree; the position
        table is small (one seat per role), so this stays cheap.
        """
        index: Dict[Optional[int], List[Position]] = {}
        positions = (
            Position.objects
            .filter(is_active=True)
            .prefetch_related("assignments__user")
        )
        for pos in positions:
            index.setdefault(pos.reports_to_id, []).append(pos)
        return index

    # ── People derivation ─────────────────────────────────────────────────────

    @staticmethod
    def _holders(position: Position) -> List[User]:
        """Active users currently holding a seat (multi-incumbent aware)."""
        return [
            a.user for a in position.assignments.all()
            if a.end_date is None and a.user and a.user.is_active
        ]

    @classmethod
    def descendant_users(cls, user: User) -> List[User]:
        """Everyone strictly beneath ``user`` in the reporting tree.

        Walks down Position.reports_to from the user's primary seat, collecting
        the holders of every descendant seat. De-duplicated; the user is never
        included even if they hold one of their own descendant seats.
        """
        position = cls.primary_position(user)
        if position is None:
            return []

        index = cls._children_index()
        out: List[User] = []
        seen_users: Set[int] = {user.pk}
        seen_positions: Set[int] = {position.pk}

        stack = list(index.get(position.pk, []))
        while stack:
            seat = stack.pop()
            if seat.pk in seen_positions:
                continue
            seen_positions.add(seat.pk)
            for holder in cls._holders(seat):
                if holder.pk not in seen_users:
                    seen_users.add(holder.pk)
                    out.append(holder)
            stack.extend(index.get(seat.pk, []))
        return out

    @classmethod
    def direct_report_users(cls, user: User) -> List[User]:
        """The holders of the seats reporting directly to the user's seat."""
        position = cls.primary_position(user)
        if position is None:
            return []
        index = cls._children_index()
        out: List[User] = []
        seen: Set[int] = {user.pk}
        for seat in index.get(position.pk, []):
            for holder in cls._holders(seat):
                if holder.pk not in seen:
                    seen.add(holder.pk)
                    out.append(holder)
        return out

    @classmethod
    def area_user_ids(cls, user: User) -> Set[int]:
        """The user plus everyone beneath them — their full scope (design: areaIds)."""
        ids = {d.pk for d in cls.descendant_users(user)}
        ids.add(user.pk)
        return ids

    @classmethod
    def is_manager(cls, user: User) -> bool:
        """True if anyone reports up to this person."""
        position = cls.primary_position(user)
        if position is None:
            return False
        return cls._children_index().get(position.pk) is not None

    @classmethod
    def can_assign(cls, manager: User, target: User) -> bool:
        """A manager may assign only to someone strictly within their area."""
        if manager.pk == target.pk:
            return False
        return any(d.pk == target.pk for d in cls.descendant_users(manager))

    @classmethod
    def direct_manager(cls, user: User) -> Optional[User]:
        """The user's line manager: the holder of the seat their primary
        position reports to. Walks up past vacant seats so a missing middle
        manager doesn't swallow escalations (e.g. completion review requests).
        Returns None at the top of the tree or when the user holds no seat.
        """
        position = cls.primary_position(user)
        if position is None:
            return None
        seen: Set[int] = {position.pk}
        seat = position.reports_to
        while seat is not None and seat.pk not in seen:
            seen.add(seat.pk)
            holder = seat.current_holder
            if holder is not None and holder.pk != user.pk:
                return holder
            seat = seat.reports_to
        return None

    @classmethod
    def chain_to(cls, user: User) -> List[User]:
        """Root → user, the line-management breadcrumb (design: chainTo).

        Built by walking up Position.reports_to and taking the primary holder of
        each seat. Stops on a vacant seat (the chain is only as deep as it is filled).
        """
        position = cls.primary_position(user)
        chain: List[User] = [user]
        if position is None:
            return chain

        seen_positions: Set[int] = {position.pk}
        seat = position.reports_to
        while seat is not None and seat.pk not in seen_positions:
            seen_positions.add(seat.pk)
            holder = seat.current_holder
            if holder is None:
                break
            chain.insert(0, holder)
            seat = seat.reports_to
        return chain
