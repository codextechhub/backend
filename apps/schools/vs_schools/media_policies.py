"""Who may read a school's logo through ``/media/``.

The logo is the one stored file that genuinely belongs to everybody in the
school: it is the sidebar, the favicon, the letterhead on an invoice a parent
opens. Restricting it further would break the shell for the users it is drawn
for, and it discloses nothing they cannot see by looking at the building.

The boundary that does matter is between schools, and the file's tenant column
settles that before this runs. What is left for this policy is the case that
column cannot see: a branding row whose school has since been moved or rebuilt
under a different tenant must not keep serving its logo to the old one.
"""
from __future__ import annotations

from core.media import register_policy

from .models import SchoolBranding


def _may_read_logo(request, branding) -> bool:
    owner_tenant = getattr(getattr(branding, "school", None), "tenant_id", None)
    caller_tenant = getattr(getattr(request, "tenant", None), "pk", None)
    return owner_tenant is not None and str(owner_tenant) == str(caller_tenant)


def register() -> None:
    register_policy(SchoolBranding, _may_read_logo)
