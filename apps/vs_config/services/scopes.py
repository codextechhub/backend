from rest_framework.exceptions import NotFound

from vs_schools.models import Branch, School

from ..constants import BRANCH_SCOPE, PLATFORM_SCOPE, SCHOOL_SCOPE
from ..exceptions import InvalidConfigurationScope


def scope_name(school=None, branch=None):
    if branch is not None:
        return BRANCH_SCOPE
    if school is not None:
        return SCHOOL_SCOPE
    return PLATFORM_SCOPE


def normalize_scope(*, school=None, branch=None):
    if branch is not None:
        if school is None:
            school = branch.school
        elif branch.school_id != school.id:
            raise InvalidConfigurationScope("Branch must belong to the selected school.")
    return school, branch


def resolve_request_scope(request, *, allow_platform=True):
    """Resolve an authorized school/branch scope without leaking foreign IDs."""
    school_ref = request.query_params.get("school") or request.data.get("school")
    branch_ref = request.query_params.get("branch") or request.data.get("branch")
    user = request.user
    is_platform_user = getattr(user, "user_type", None) == "CX_STAFF"

    school = None
    if school_ref:
        school = School.objects.filter(pk=school_ref).first()
        if school is None:
            raise NotFound("Configuration scope not found.")
    elif not is_platform_user:
        school = getattr(request, "school", None) or getattr(user, "school", None)

    if not is_platform_user:
        user_school = getattr(user, "school", None)
        if school is None or user_school is None or school.pk != user_school.pk:
            raise NotFound("Configuration scope not found.")
    elif school is None and not allow_platform:
        raise InvalidConfigurationScope("A school scope is required.")

    branch = None
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
        branch = getattr(user, "branch", None)

    return normalize_scope(school=school, branch=branch)
