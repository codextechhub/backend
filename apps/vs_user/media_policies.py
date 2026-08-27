"""Who may read a staff photo through ``/media/``.

``PlatformStaffProfile`` photos are the faces on the CodeX organogram. Within
the platform tenant they are directory data, not a secret: the org chart, the
assignee picker and the avatars beside a comment all show colleagues to each
other, and a photo only its owner can load renders every one of those screens as
grey circles. So inside the tenant the answer is yes.

Outside it the answer is no, and the file's own tenant column already decides
that before this runs (``core.media.authorize``). What is left for this policy is
the case that column cannot see: a profile whose user has since moved tenant must
not keep serving its photo to the tenant they left.
"""
from __future__ import annotations

from core.media import register_policy

from .models import PlatformStaffProfile


def _may_read_profile_photo(request, profile) -> bool:
    owner_tenant = getattr(getattr(profile, "user", None), "tenant_id", None)
    caller_tenant = getattr(getattr(request, "tenant", None), "pk", None)
    return owner_tenant is not None and str(owner_tenant) == str(caller_tenant)


def register() -> None:
    register_policy(PlatformStaffProfile, _may_read_profile_photo)
