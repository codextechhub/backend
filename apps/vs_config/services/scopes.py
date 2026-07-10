from rest_framework.exceptions import NotFound

from vs_schools.models import Branch, School

from ..constants import BRANCH_SCOPE, PLATFORM_SCOPE, SCHOOL_SCOPE
from ..exceptions import InvalidConfigurationScope


# Collapse school/branch objects into the persisted configuration scope name.
def scope_name(school=None, branch=None):
    if branch is not None:
        return BRANCH_SCOPE
    if school is not None:
        return SCHOOL_SCOPE
    # Absence of tenant objects means the value belongs to the platform default layer.
    return PLATFORM_SCOPE


# Keep branch-scoped writes tied to their owning school before keys are built.
def normalize_scope(*, school=None, branch=None):
    if branch is not None:
        if school is None:
            # Branch-scoped values still persist school for filtering and audit reporting.
            school = branch.school
        elif branch.school_id != school.id:
            raise InvalidConfigurationScope("Branch must belong to the selected school.")
    return school, branch


# Resolve request-provided scope references through the caller's allowed tenancy.
def resolve_request_scope(request, *, allow_platform=True):
    """Resolve an authorized school/branch scope without leaking foreign IDs."""
    school_ref = request.query_params.get("school") or request.data.get("school")
    branch_ref = request.query_params.get("branch") or request.data.get("branch")
    user = request.user
    is_platform_user = getattr(user, "user_type", None) == "CX_STAFF"

    school = None
    if school_ref:
        # All missing/foreign scope failures return the same 404 to avoid tenant enumeration.
        school = School.objects.filter(pk=school_ref).first()
        if school is None:
            raise NotFound("Configuration scope not found.")
    elif not is_platform_user:
        # School users inherit their own school when the client omits explicit scope.
        school = getattr(request, "school", None) or getattr(user, "school", None)

    # Non-platform users may only resolve their own school, even if a foreign ID exists.
    if not is_platform_user:
        user_school = getattr(user, "school", None)
        if school is None or user_school is None or school.pk != user_school.pk:
            raise NotFound("Configuration scope not found.")
    elif school is None and not allow_platform:
        # Some write paths require a tenant layer and must not fall back to platform.
        raise InvalidConfigurationScope("A school scope is required.")

    branch = None
    # Branch lookups are filtered through the resolved school to avoid cross-school leaks.
    if branch_ref:
        qs = Branch.all_objects.filter(pk=branch_ref)
        if school is not None:
            qs = qs.filter(school=school)
        branch = qs.first()
        if branch is None:
            raise NotFound("Configuration scope not found.")
        if not is_platform_user:
            user_branch = getattr(user, "branch", None)
            if user_branch is not None and branch.pk != user_branch.pk:
                raise NotFound("Configuration scope not found.")
    elif not is_platform_user and getattr(user, "user_type", None) != "SCHOOL_ADMIN":
        # Branch users default to their branch; school admins remain at school scope.
        branch = getattr(user, "branch", None)

    return normalize_scope(school=school, branch=branch)
