"""The platform team and sign-in monitoring screens the search bar can route to.

Every key here is the platform's own, so only platform staff see these places,
in the console. The live sessions and login attempts lists read with
``platform.security.view``, which the platform roles hold. Account lockouts,
password activity and the platform organogram admit any platform staff member
without a key (:class:`vs_rbac.permissions.IsVisionStaff`), so their places
need no key and name the ``platform_staff`` condition instead.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

SECURITY_VIEW = "platform.security.view"
PLATFORM_STAFF = "platform_staff"

register(
    Place(
        kind=ACTION, key="user.platform_team", label="CX users",
        description="The platform's own staff accounts.", keywords=("team", "staff"),
        screen="user.platform_team", permission="platform.team.view",
    ),
    Place(
        kind=ACTION, key="user.sessions", label="Live sessions",
        description="Who is signed in, on which device and from where.",
        keywords=("sessions", "signed in", "devices", "security"),
        screen="user.sessions", permission=SECURITY_VIEW,
    ),
    Place(
        kind=ACTION, key="user.login_attempts", label="Login attempts",
        description="Every sign-in attempt, successful or failed.",
        keywords=("sign in", "failed logins", "security"),
        screen="user.login_attempts", permission=SECURITY_VIEW,
    ),
    Place(
        kind=ACTION, key="user.lockouts", label="Account lockouts",
        description="Accounts locked after failed sign-ins, and unlocking them.",
        keywords=("locked out", "unlock", "security"),
        screen="user.lockouts", condition=PLATFORM_STAFF,
    ),
    Place(
        kind=ACTION, key="user.password_activity", label="Password activity",
        description="Password reset links issued, used and revoked.",
        keywords=("password reset", "security"),
        screen="user.password_activity", condition=PLATFORM_STAFF,
    ),
    Place(
        kind=ACTION, key="user.organogram", label="CX organogram",
        description="The platform's departments, positions and who holds them.",
        keywords=("org chart", "departments", "positions"),
        screen="user.organogram", condition=PLATFORM_STAFF,
    ),
)
