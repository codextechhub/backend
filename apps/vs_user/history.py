"""The account and CX staff rows whose history a profile can be read at.

An account is tracked by an allow-list, never by exclusion: it holds the
password hash and the sign-in bookkeeping, none of which belongs in a history
that everyone who may open the person's record can read. Only the identity a
profile shows is kept: the name, the contact details, the gender, the branch
and the account's status.

A CX staff member's HR profile is tracked whole. Its payroll bank fields are
kept like the rest and reach a reader only through the serializer, which
applies Field Access to a past version exactly as it does to the live one.
"""
from vs_history.registry import track

USER_HISTORY_FIELDS = (
    "first_name", "last_name", "email", "phone", "gender", "status",
    "branch", "is_active", "created_at",
)


def register():
    """Declare the tracked account-side models to the history engine."""
    from .models import PlatformStaffProfile, User

    track(User, fields=USER_HISTORY_FIELDS)
    track(PlatformStaffProfile)
