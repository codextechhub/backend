"""Entitlement windows as a caller's Field Access lets them read them.

A school's plan writes the window of each module grant it pays for: the grant
starts when the subscription starts and ends when it expires
(``source=PACKAGE``). Those two dates are the school's registered
``platform.schools.subscription_starts_at`` and
``platform.schools.subscription_expires_at``, so a caller who may not read them
on the school's record must not read them off the entitlement list, the renewal
calendar or the response to a schedule either.

A grant written by hand (``MANUAL``, ``PLATFORM``, ``IMPORT``) has a window of
its own that is not the subscription's, and is open to everyone who may read
entitlements.

CodeX staff reach a school's entitlements by asserting the school as the
request's tenant; the shared Field Access door judges the subscription switches,
which are platform fields, in the operator's own tenant.
"""
from __future__ import annotations

from vs_rbac.field_enforcement import can_read

from .models import CapabilityEntitlement

STARTS_KEY = "platform.schools.subscription_starts_at"
EXPIRES_KEY = "platform.schools.subscription_expires_at"


def window_readable(request, source: str) -> tuple[bool, bool]:
    """Whether the caller may read ``(starts_at, ends_at)`` of a grant from *source*."""
    if not can_read(request, "platform.entitlements.window"):
        return False, False
    if source != CapabilityEntitlement.Source.PACKAGE:
        return True, True
    return can_read(request, STARTS_KEY), can_read(request, EXPIRES_KEY)
