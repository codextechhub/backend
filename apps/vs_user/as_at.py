"""A CX staff profile as it stood at the end of an earlier day.

The profile and its account are rebuilt from :mod:`vs_history` and rendered
through the live serializer, so Field Access and the owner rule apply to the
past exactly as they do to the present.

What a past view shows differently: the seat the person held is the one the
profile named that day, while the unit, department, division and line manager
around that seat are read from the organisation as it stands now, because the
organogram keeps its own effective-dated history (``PositionAssignment``)
rather than this one. A photograph replaced since is not shown, because its
file was retired when it was replaced.
"""
from __future__ import annotations

from vs_history.as_at import AsAt, history_starts, instance_at, require_history
from vs_history.registry import spec_for

from .models import PlatformStaffProfile, User


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
    meta = {
        "date": as_at.date.isoformat(),
        "history_starts": starts.isoformat(),
        "photo_retired": photo_retired,
    }
    return record, meta
