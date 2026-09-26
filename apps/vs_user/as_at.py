"""A CX staff profile as it stood at the end of an earlier day.

The profile and its account are rebuilt from :mod:`vs_history` and rendered
through the live serializer, so Field Access and the owner rule apply to the
past exactly as they do to the present.

The organisation around the person is composed for the same day. The seat is
the primary one ``PositionAssignment`` shows them holding that day (or the one
the profile named, where no assignment covers it). The unit, department and
division are that seat's unit and its ancestors as each stood that day, and
the line manager is whoever held the seat it reported to that day. Seats and
units keep a history of their own, which starts when tracking reached them; on
a day before that the unit, department, division and line manager are left
empty and ``organisation_history_starts`` in the ``as_at`` block names the
first day they can be answered for. They are never filled in from today's
organisation.

A photograph replaced since is not shown, because its file was retired when it
was replaced.
"""
from __future__ import annotations

from django.db.models import Q

from vs_history.as_at import (
    AsAt,
    history_starts,
    instance_at,
    require_history,
    tracking_starts,
)
from vs_history.registry import spec_for

from .models import OrgNode, PlatformStaffProfile, Position, PositionAssignment, User

#: How far up the org tree a unit's ancestors are followed.
_MAX_DEPTH = 12


def profile_history_starts(profile_pk):
    return history_starts(spec_for(PlatformStaffProfile), profile_pk)


def profile_at(profile, as_at: AsAt):
    """*profile* rebuilt as at *as_at*, and the ``as_at`` block for the response."""
    spec = spec_for(PlatformStaffProfile)
    name = " ".join(p for p in (profile.user.first_name, profile.user.last_name) if p)
    starts = require_history(spec, profile.pk, as_at, noun=f"{name}'s profile")
    record = instance_at(spec, profile.pk, as_at)
    record.updated_at = record._history_version.recorded_at
    account = instance_at(spec_for(User), profile.user_id, as_at)
    if account is not None:
        account.tenant_id = profile.user.tenant_id
        record.user = account
    else:
        record.user = profile.user
    photo_retired = bool(record.profile_photo.name) and (
        record.profile_photo.name != (profile.profile_photo.name or "")
    )
    if photo_retired:
        record.profile_photo = None
    org_starts = organisation_starts()
    _install_organisation(record, profile.user_id, as_at, org_starts)
    meta = {
        "date": as_at.date.isoformat(),
        "history_starts": starts.isoformat(),
        "photo_retired": photo_retired,
        "organisation_history_starts": org_starts.isoformat() if org_starts else None,
    }
    return record, meta


def organisation_starts():
    """The first day both seats and units can be read as at, or ``None``."""
    seats = tracking_starts(spec_for(Position))
    units = tracking_starts(spec_for(OrgNode))
    return max(seats, units) if seats and units else None


def _in_force(queryset, as_at: AsAt):
    """Assignments held at the end of *as_at*: begun by then, not yet ended.

    A tenure closed on a day ends that day, so a seat changed on 3 March shows
    the new holder, not the old one, on 3 March.
    """
    day = as_at.date
    return queryset.filter(start_date__lte=day).filter(
        Q(end_date__isnull=True) | Q(end_date__gt=day),
    )


def _seat_at(user_pk, fallback_pk, as_at: AsAt):
    """The id of the primary seat *user_pk* held at *as_at*."""
    held = (
        _in_force(PositionAssignment.objects.filter(user_id=user_pk, is_primary=True), as_at)
        .order_by("-start_date", "-id")
        .values_list("position_id", flat=True)
        .first()
    )
    return held or fallback_pk


def _holder_at(position_pk, as_at: AsAt):
    """Who held seat *position_pk* at *as_at*, named as they were that day."""
    assignment = (
        _in_force(PositionAssignment.objects.filter(position_id=position_pk), as_at)
        .order_by("-is_primary", "-start_date", "-id")
        .select_related("user")
        .first()
    )
    if assignment is None:
        return None
    then = instance_at(spec_for(User), assignment.user_id, as_at)
    return then if then is not None else assignment.user


def _unit_chain_at(node_pk, as_at: AsAt):
    """The unit *node_pk* as it stood, with each ancestor installed as its parent."""
    spec = spec_for(OrgNode)
    node = instance_at(spec, node_pk, as_at) if node_pk else None
    current, depth = node, 0
    while current is not None and depth < _MAX_DEPTH:
        parent = instance_at(spec, current.parent_id, as_at) if current.parent_id else None
        current._state.fields_cache["parent"] = parent
        current, depth = parent, depth + 1
    return node


def _install_organisation(record, user_pk, as_at: AsAt, org_starts):
    """Put the seat, its unit chain and the line manager of *as_at* on *record*.

    Before the organisation's history starts the seat is still named (it is
    the profile's own, or dated by an assignment), but the unit around it and
    the line manager are left empty rather than taken from today.
    """
    seat_pk = _seat_at(user_pk, record.position_id, as_at)
    record.position_id = seat_pk
    known = org_starts is not None and as_at.date >= org_starts
    seat = instance_at(spec_for(Position), seat_pk, as_at) if seat_pk and known else None
    if seat is None and seat_pk:
        seat = Position.objects.filter(pk=seat_pk).first()
        if seat is not None and not known:
            seat._state.fields_cache["org_node"] = None
            seat.org_node_id = None
    if seat is not None and known:
        seat._state.fields_cache["org_node"] = _unit_chain_at(seat.org_node_id, as_at)
    if seat is None:
        record.position_id = None
    record._state.fields_cache["position"] = seat
    manager = None
    if known and seat is not None and seat.reports_to_id:
        manager = _holder_at(seat.reports_to_id, as_at)
    record.__dict__["_line_manager_as_at"] = manager
