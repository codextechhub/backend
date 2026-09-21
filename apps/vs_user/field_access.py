"""Staff account fields an administrator may restrict per role.

``platform.staff_profile`` holds a CX staff member's payroll bank details,
written by the profile endpoints. All three are declared sensitive and
``PLATFORM`` scope, so only a platform role reaches them and only where the
switches are on. A staff member always reads and writes their own, whatever
their roles say: that is an owner rule on the serializer, not a switch.

``platform.team`` holds account security and invitation facts about another
user. Reaching the account at all is ``platform.team.view``, which a school's
own administrators hold too; which of these facts they see is the switch. The
server records every one of them (sign-in, password change, invitation), so
none is writable.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"
_PLATFORM = "PLATFORM"


def register():
    """Publish the staff account field declarations to the Field Access registry."""
    register_fields(
        "platform",
        "staff_profile",
        surfaces=("vs_user.serializers.PlatformStaffProfileSerializer",),
        fields=(
            FieldSpec("bank_name", "Bank name", group="Banking", sensitive=True,
                      scope=_PLATFORM, sort_order=10),
            FieldSpec("account_name", "Account name", group="Banking", sensitive=True,
                      scope=_PLATFORM, sort_order=20),
            FieldSpec("account_number", "Account number", group="Banking",
                      sensitive=True, scope=_PLATFORM, sort_order=30),
        ),
    )
    register_fields(
        "platform",
        "team",
        surfaces=(
            "vs_user.serializers.UserReadSerializer",
            "vs_user.serializers.UserListSerializer",
        ),
        fields=(
            FieldSpec("password_changed_at", "Password last changed",
                      group="Account security", sensitive=True, writable=False,
                      scope=_TENANT, sort_order=10),
            FieldSpec("last_login_at", "Last signed in", group="Account security",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=20),
            FieldSpec("invited_by", "Invited by", group="Invitation", sensitive=True,
                      writable=False, scope=_TENANT, sort_order=10,
                      api_names=("invited_by_id", "invited_by_name")),
            FieldSpec("invitation_email_status", "Invitation email status",
                      group="Invitation", sensitive=True, writable=False,
                      scope=_TENANT, sort_order=20),
            FieldSpec("invitation_expires_at", "Invitation expires",
                      group="Invitation", sensitive=True, writable=False,
                      scope=_TENANT, sort_order=30),
        ),
    )
