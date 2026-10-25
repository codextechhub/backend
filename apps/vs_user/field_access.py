"""Staff account fields an administrator may restrict per role.

``platform.staff_profile`` holds a CX staff member's payroll bank details,
written by the profile endpoints. All three are declared sensitive and
``PLATFORM`` scope, so only a platform role reaches them and only where the
switches are on. A staff member always reads and writes their own, whatever
their roles say: that is an owner rule on the serializer, not a switch.

The rest of the profile (the name, the personal and contact details, the next
of kin and the employment details) is open: everybody who may open a profile
reads it today, and a switch that started closed would hide it from every role
the day it shipped. Each has a switch because each is a fact the platform may
be asked about later. The name is held on the account, so it reaches the
profile nested under ``user``, and correcting it on another person's account
asks the same switch (``UserUpdateSerializer``).
Adding a member of staff sets the details the Add form and the spreadsheet
import carry as the record is created, and each is judged by its Write switch
(``UserCreateSerializer``); the name is open on create, because nobody can be
added without one.

``platform.team`` holds account security and invitation facts about another
user. Reaching the account at all is ``platform.team.view``, which a school's
own administrators hold too; which of these facts they see is the switch. The
server records every one of them (sign-in, password change, invitation), so
none is writable.

``school.account_closures`` holds the facts of a person's request to close
their own account, as a reviewer reads them. The server sets all of them, so
none is writable and none is sensitive: a reviewer who may open the request
reads who asked, how, and what was decided, until a school switches a field off
for a role. The branch is the reviewer's own scope and is not a switch.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"
_PLATFORM = "PLATFORM"


def register():
    """Publish the staff account field declarations to the Field Access registry."""
    register_fields(
        "platform",
        "staff_profile",
        surfaces=(
            "vs_user.serializers.PlatformStaffProfileSerializer",
            "vs_user.serializers.PlatformStaffProfileListSerializer",
            "vs_user.serializers.PlatformStaffProfileBriefSerializer",
            # The account block nested in each of them, which carries the name.
            "vs_user.serializers.StaffProfileAccountSerializer",
        ),
        fields=(
            FieldSpec("first_name", "First name", group="Name", scope=_PLATFORM,
                      sort_order=10, open_on_create=True,
                      description="Held on the staff member's account."),
            FieldSpec("last_name", "Last name", group="Name", scope=_PLATFORM,
                      sort_order=20, open_on_create=True,
                      description="Held on the staff member's account."),
            FieldSpec("date_of_birth", "Date of birth", group="Personal",
                      scope=_PLATFORM, sort_order=10),
            FieldSpec("marital_status", "Marital status", group="Personal",
                      scope=_PLATFORM, sort_order=20),
            FieldSpec("nationality", "Nationality", group="Personal",
                      scope=_PLATFORM, sort_order=30),
            FieldSpec("state_of_origin", "State of origin", group="Personal",
                      scope=_PLATFORM, sort_order=40),
            FieldSpec("profile_photo", "Photo", group="Personal", scope=_PLATFORM,
                      sort_order=50),
            FieldSpec("personal_email", "Personal email", group="Contact",
                      scope=_PLATFORM, sort_order=10),
            FieldSpec("alternate_phone", "Alternate phone", group="Contact",
                      scope=_PLATFORM, sort_order=20),
            FieldSpec("residential_address", "Residential address", group="Contact",
                      scope=_PLATFORM, sort_order=30),
            FieldSpec("city", "City", group="Contact", scope=_PLATFORM, sort_order=40),
            FieldSpec("state", "State", group="Contact", scope=_PLATFORM, sort_order=50),
            FieldSpec("nok_name", "Next of kin name", group="Next of kin",
                      scope=_PLATFORM, sort_order=10),
            FieldSpec("nok_relationship", "Next of kin relationship",
                      group="Next of kin", scope=_PLATFORM, sort_order=20),
            FieldSpec("nok_phone", "Next of kin phone", group="Next of kin",
                      scope=_PLATFORM, sort_order=30),
            FieldSpec("nok_address", "Next of kin address", group="Next of kin",
                      scope=_PLATFORM, sort_order=40),
            FieldSpec("employee_id", "Employee ID", group="Employment",
                      scope=_PLATFORM, sort_order=10),
            FieldSpec("job_title", "Job title", group="Employment", scope=_PLATFORM,
                      sort_order=20),
            FieldSpec("employment_type", "Employment type", group="Employment",
                      scope=_PLATFORM, sort_order=30),
            FieldSpec("employment_status", "Employment status", group="Employment",
                      scope=_PLATFORM, sort_order=40),
            FieldSpec("date_joined", "Date joined", group="Employment",
                      scope=_PLATFORM, sort_order=50),
            FieldSpec("date_exited", "Date exited", group="Employment",
                      scope=_PLATFORM, sort_order=60),
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
    register_fields(
        "school",
        "account_closures",
        surfaces=("vs_user.serializers.AccountClosureRequestSerializer",),
        fields=(
            FieldSpec("person", "Person", group="Request", writable=False,
                      scope=_TENANT, sort_order=10,
                      api_names=("user_id", "user_name", "user_email"),
                      description="Who asked to close their account."),
            FieldSpec("kind", "Account type", group="Request", writable=False,
                      scope=_TENANT, sort_order=20),
            FieldSpec("channel", "How it was asked", group="Request", writable=False,
                      scope=_TENANT, sort_order=30),
            FieldSpec("requested_at", "Asked on", group="Request", writable=False,
                      scope=_TENANT, sort_order=40),
            FieldSpec("status", "Status", group="Decision", writable=False,
                      scope=_TENANT, sort_order=10),
            FieldSpec("decided_at", "Decided on", group="Decision", writable=False,
                      scope=_TENANT, sort_order=20),
            FieldSpec("decided_by", "Decided by", group="Decision", writable=False,
                      scope=_TENANT, sort_order=30,
                      api_names=("decided_by_id", "decided_by_name")),
        ),
    )
