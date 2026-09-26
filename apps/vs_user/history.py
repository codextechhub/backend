"""The account and CX staff rows whose history a profile can be read at.

An account is tracked by an allow-list, never by exclusion: it holds the
password hash and the sign-in bookkeeping, none of which belongs in a history
that everyone who may open the person's record can read. Only the identity a
profile shows is kept: the name, the contact details, the gender, the branch
and the account's status.

A CX staff member's HR profile is tracked whole. Its payroll bank fields are
kept like the rest and reach a reader only through the serializer, which
applies Field Access to a past version exactly as it does to the live one.

The organogram's seats and units are tracked for their shape: which unit a
seat sits in, which seat it reports to, and each unit's name, tier and parent.
Who held a seat is already effective-dated on ``PositionAssignment``, so
together they say which team, department, division and line manager a person
had on an earlier day.
"""
from vs_history.registry import track

USER_HISTORY_FIELDS = (
    "first_name", "last_name", "email", "phone", "gender", "status",
    "branch", "is_active", "created_at",
)


def register():
    """Declare the tracked account-side models to the history engine."""
    from .models import OrgNode, PlatformStaffProfile, Position, User

    track(User, fields=USER_HISTORY_FIELDS)
    track(PlatformStaffProfile)
    track(OrgNode, fields=("name", "code", "kind", "parent", "is_active"))
    track(Position, fields=("title", "code", "org_node", "reports_to", "is_active"))
