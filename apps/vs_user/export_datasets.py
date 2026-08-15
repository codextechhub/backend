"""Administration datasets published to the Export Centre.

Registered from :meth:`vs_user.apps.VsUserConfig.ready`. All tenant-scoped: people and
their access belong to the organisation, not to a set of books.

Everything here is about *who can do what*, which is why the columns that identify a
person - email, phone, date of birth - are restricted even though the row itself is
readable. An access review needs the roster; it does not need everyone's phone number.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_BOOLEAN,
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_SEARCH,
    FILTER_TEXT,
    KIND_CHOICE,
    KIND_DATETIME,
    KIND_TEXT,
    Dataset,
    DatasetScope,
    Field,
    FilterDef,
    choice_labels,
    register,
)


# Build the base queryset for user accounts.
#
# This mirrors UserListView exactly, and it has to: the Users console is a
# PLATFORM console. Its School Users tab lists accounts belonging to school
# tenants - other tenants than the caller's - and a dataset fenced to
# `tenant=scope.tenant` returned only the platform tenant's own CX staff, so
# exporting that tab produced an empty file while the table showed people.
#
# The fence still exists for everyone else. Only a caller whose own tenant is of
# kind PLATFORM reads across tenants, which is the same condition the list
# endpoint applies and the same one that already governs what they can see on
# screen. A school-tenant caller exporting users still gets only their own.
def _users(scope):
    from vs_tenants.models import Tenant

    from .models import User

    qs = User.objects.all()
    if getattr(getattr(scope, "tenant", None), "kind", None) != Tenant.Kind.PLATFORM:
        qs = qs.filter(tenant=scope.tenant)
    # The endpoint hides these two everywhere; an export that showed them would
    # not match any screen in the console.
    return qs.exclude(status__in=[User.Status.PENDING_APPROVAL, User.Status.REJECTED])


# Build the tenant-scoped base queryset for role assignments.
def _role_assignments(scope):
    from vs_rbac.models import TenantUserRoleAssignment

    return TenantUserRoleAssignment.objects.filter(tenant=scope.tenant)


# Build the tenant-scoped base queryset for sign-in sessions.
def _login_sessions(scope):
    from .models import LoginSession

    # all_objects: the default manager is tenant-aware and would double-scope.
    return LoginSession.all_objects.filter(tenant=scope.tenant)


_USER_TYPE = choice_labels("vs_user.models.User.UserType")
_TENANT_KIND = choice_labels("vs_tenants.models.Tenant.Kind")
_USER_STATUS = choice_labels("vs_user.models.User.Status")


# Register every administration dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="admin.users",
        module="Administration",
        name="User accounts",
        description=(
            "Everyone with an account, with their type, status and the school they "
            "belong to. The roster an access review starts from. A platform caller "
            "sees every tenant's users, exactly as the Users console does; anyone "
            "else sees only their own. Contact columns are restricted."
        ),
        base=_users,
        scope=DatasetScope.TENANT,
        permission="platform.team.view",
        row_cap=100_000,
        default_columns=("email", "first_name", "last_name", "user_type", "status"),
        fields=(
            Field("email", "Email", "Account", KIND_TEXT, locked=True,
                  description="The account's identity - always exported."),
            Field("first_name", "First name", "Person", KIND_TEXT),
            Field("last_name", "Last name", "Person", KIND_TEXT),
            Field("user_type", "Type", "Account", KIND_CHOICE, choices=_USER_TYPE),
            Field("status", "Status", "Account", KIND_CHOICE, choices=_USER_STATUS),
            Field("is_active", "Active", "Account", KIND_TEXT),
            Field("last_login", "Last signed in", "Account", KIND_DATETIME),
            Field("created_at", "Created", "Record", KIND_DATETIME),
            Field("phone", "Phone", "Person", KIND_TEXT, sensitive=True,
                  description="Restricted: personal contact data."),
            # This export can span tenants for a platform caller, so a row is
            # ambiguous without saying which school it belongs to.
            Field("school", "School", "Placement", KIND_TEXT,
                  source="tenant__school_profile__name"),
            Field("school_code", "School code", "Placement", KIND_TEXT,
                  source="tenant__school_profile__code"),
            Field("tenant_kind", "Tenant type", "Placement", KIND_CHOICE,
                  source="tenant__kind", choices=_TENANT_KIND),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("user_type", "Type", FILTER_CHOICE, choices=_USER_TYPE),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_USER_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("email", "Email"), ("first_name", "First name"),
                ("last_name", "Last name"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("email", "Email", FILTER_TEXT),
            # The console's CX / School tabs are a tenant-KIND split, not a
            # user_type one - CX staff live in the platform tenant. Filtering on
            # the kind is what makes the export agree with the tab.
            FilterDef("tenant_kind", "Tenant type", FILTER_CHOICE,
                      source="tenant__kind", choices=_TENANT_KIND),
            FilterDef("school", "School", FILTER_TEXT,
                      source="tenant__school_profile__name"),
        ),
    ))

    register(Dataset(
        key="admin.role_assignments",
        module="Administration",
        name="Role assignments",
        description=(
            "Who holds which role, when it was granted and by whom - including "
            "revoked grants. The answer to 'who could approve payments last March'."
        ),
        base=_role_assignments,
        scope=DatasetScope.TENANT,
        permission="platform.roles.view",
        row_cap=200_000,
        default_columns=("user_email", "role_name", "assignment_status", "assigned_at"),
        fields=(
            Field("assignment_id", "Assignment", "Assignment", KIND_TEXT, source="id",
                  locked=True),
            Field("user_email", "User", "Assignment", KIND_TEXT, source="user__email"),
            Field("role_key", "Role key", "Assignment", KIND_TEXT, source="role__key"),
            Field("role_name", "Role", "Assignment", KIND_TEXT, source="role__name"),
            Field("assignment_status", "Status", "Assignment", KIND_TEXT),
            Field("assigned_at", "Granted", "Assignment", KIND_DATETIME),
            Field("assigned_by", "Granted by", "Assignment", KIND_TEXT,
                  source="assigned_by__email"),
            Field("revoked_at", "Revoked", "Assignment", KIND_DATETIME),
            Field("revoked_by", "Revoked by", "Assignment", KIND_TEXT,
                  source="revoked_by__email"),
        ),
        filters=(
            FilterDef("assigned_at", "Granted", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("assignment_status", "Status", FILTER_TEXT),
            FilterDef("role", "Role", FILTER_TEXT, source="role__name"),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("user__email", "User"),
                ("role__name", "Role"),
                ("role__key", "Role key"),
            ), description="Matches any one of these, the way the search box does."),
        ),
    ))

    register(Dataset(
        key="admin.sign_ins",
        module="Administration",
        name="Sign-in sessions",
        description=(
            "Every session opened against this organisation, with device and outcome. "
            "IP address and user agent are restricted."
        ),
        base=_login_sessions,
        scope=DatasetScope.TENANT,
        permission="platform.team.view",
        row_cap=500_000,
        default_columns=("user_email", "last_seen_at", "is_active"),
        fields=(
            Field("session_id", "Session", "Session", KIND_TEXT, source="id", locked=True),
            Field("user_email", "User", "Session", KIND_TEXT, source="user__email"),
            Field("last_seen_at", "Last seen", "Session", KIND_DATETIME),
            Field("ended_at", "Ended", "Session", KIND_DATETIME),
            Field("end_reason", "Why it ended", "Session", KIND_TEXT),
            Field("is_active", "Still active", "Session", KIND_TEXT),
            Field("device_label", "Device label", "Device", KIND_TEXT),
            Field("ip_address", "IP address", "Device", KIND_TEXT, sensitive=True,
                  description="Restricted: identifies where a person signed in from."),
            Field("user_agent", "Device", "Device", KIND_TEXT, sensitive=True,
                  description="Restricted: identifies a person's device."),
        ),
        filters=(
            FilterDef("last_seen_at", "Last seen", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            FilterDef("user", "User", FILTER_TEXT, source="user__email"),
            FilterDef("is_active", "Still active", FILTER_BOOLEAN),
            FilterDef("end_reason", "Why it ended", FILTER_TEXT),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("user__email", "User"),
                ("device_label", "Device label"),
            ), description="Matches any one of these, the way the search box does."),
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the user list screen's filters into export filters.
def _translate_users(params):
    from vs_exports.catalogue import Unmapped
    from vs_tenants.models import Tenant

    from .models import User

    filters, unmapped = [], []
    if value := params.get("user_type"):
        filters.append({"id": "user_type", "values": [value]})
    elif scope := params.get("scope"):
        # The screen's two tabs are "CX" and "School", and that split is by tenant
        # KIND, not user type: CX staff are the platform tenant's users. An earlier
        # version mapped this to "every user_type except CX_STAFF", which is a
        # different question and produced an empty file - the rows it wanted live
        # in other tenants entirely. Filter the same column the list view does.
        if scope == "school":
            filters.append({"id": "tenant_kind", "values": [
                str(k) for k in Tenant.Kind.values if k != Tenant.Kind.PLATFORM
            ]})
        else:
            # Any other scope is one nobody wrote a rule for, and a scope that
            # narrows the screen but not the export is exactly the silent
            # widening this reports rather than swallows.
            unmapped.append(Unmapped(
                "scope", scope,
                "This view is not one the user export recognises, so the file is not "
                "limited by it.",
            ))
    if value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    elif excluded := params.get("exclude_status"):
        # The Members tab hides drafts and unapproved accounts with
        # `exclude_status=PENDING,DRAFT`. The export filter is "is any of", so
        # the exclusion is carried as its complement. Without this the default
        # view of the busiest user screen in the console warns on every open.
        drop = {v.strip() for v in str(excluded).split(",") if v.strip()}
        # The base already withholds these two everywhere, so naming them in the
        # complement would put them in the readable summary of the file
        # ("Status is any of ... Creation Rejected") while the rows never appear.
        drop |= {str(User.Status.PENDING_APPROVAL), str(User.Status.REJECTED)}
        keep = [str(s) for s in User.Status.values if str(s) not in drop]
        if keep:
            filters.append({"id": "status", "values": keep})
        else:
            unmapped.append(Unmapped(
                "exclude_status", excluded,
                "This view excludes every status the export knows about, so it cannot "
                "be expressed as a filter.",
            ))
    for key in ("q", "search"):
        if value := params.get(key):
            filters.append({"id": "search", "value": value})
            break
    if (value := params.get("branch")) is not None:
        unmapped.append(Unmapped(
            "branch", value,
            "The user export does not filter by branch yet; the file covers the whole "
            "organisation.",
        ))
    if (value := params.get("role")) is not None:
        unmapped.append(Unmapped(
            "role", value,
            "Export Role assignments instead - that dataset is per grant, so it can "
            "filter by role.",
        ))
    return filters, unmapped


# Translate the role-assignments screen's filters into export filters.
def _translate_role_assignments(params):
    filters, unmapped = [], []
    # The screen sends "all" to mean no filter, not a status called "all".
    if (value := params.get("assignment_status")) and value != "all":
        filters.append({"id": "assignment_status", "value": value})
    if (value := params.get("role")) and value != "all":
        filters.append({"id": "role", "value": value})
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    return filters, unmapped


# Translate the sign-in sessions screen's filters into export filters.
def _translate_sign_ins(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    if value := params.get("is_active"):
        filters.append({"id": "is_active", "value": str(value).lower() == "true"})
    if value := params.get("end_reason"):
        filters.append({"id": "end_reason", "value": value})
    if value := params.get("school"):
        unmapped.append(Unmapped(
            "school", value,
            "The sign-ins export does not carry a school filter; the file covers every "
            "school the other filters allow.",
        ))
    if value := params.get("ended_today"):
        unmapped.append(Unmapped(
            "ended_today", value,
            "The export filters on when a session was last seen, not on when it ended. "
            "Set the Last seen range in the builder to narrow it.",
        ))
    return filters, unmapped


# Register the administration screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="admin.users",
        handles=(
            "scope", "user_type", "status", "exclude_status", "q", "search",
            "branch", "role",
        ),
        label="Administration - Users",
        dataset_key="admin.users",
        translate=_translate_users,
    ))
    register_screen(ScreenBinding(
        key="admin.role_assignments",
        handles=(
            "assignment_status", "role", "search",
        ),
        label="Administration - Role assignments",
        dataset_key="admin.role_assignments",
        translate=_translate_role_assignments,
    ))
    register_screen(ScreenBinding(
        key="admin.sign_ins",
        handles=(
            "search", "is_active", "end_reason", "school", "ended_today",
        ),
        label="Administration - Sign-in sessions",
        dataset_key="admin.sign_ins",
        translate=_translate_sign_ins,
        # A security review looks at the recent past, and sessions are high
        # volume; 90 days matches the audit console.
        default_window_days=90,
    ))
