"""
Approver resolution — resolves who can act on a stage right now.
Honoring: C1 (scope), C2 (delegation), C4 (requester blocked).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from django.db.models import Q
from django.utils import timezone

from vs_workflow.constants import ApproverScope
from vs_workflow.models import ApprovalDelegation, WorkflowInstance, WorkflowStage

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


@dataclass
class EligibleApprover:
    """A single eligible approver, optionally acting on behalf of a delegator."""
    user: AbstractBaseUser
    on_behalf_of: Optional[AbstractBaseUser] = None


def _users_with_permission(school, branch, permission_key: str, scope: ApproverScope):
    """
    vs_rbac integration boundary. Edit this import to match your RBAC API.
    Expected: resolve_users_with_permission(school, branch, permission_key) -> QuerySet
    """
    try:
        from vs_rbac.evaluator import resolve_users_with_permission
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "vs_rbac not available; returning unscoped user set. Connect vs_rbac.")
        from django.contrib.auth import get_user_model
        qs = get_user_model().objects.filter(is_active=True)
        if school is not None and hasattr(User, "school"):
            qs = qs.filter(school=school)
        return qs

    if scope == ApproverScope.PLATFORM:
        school_arg, branch_arg = None, None
    elif scope == ApproverScope.BRANCH:
        school_arg, branch_arg = school, branch
    else:  # SCHOOL
        school_arg, branch_arg = school, None
    return resolve_users_with_permission(
        school=school_arg, branch=branch_arg, permission_key=permission_key,
    )


def resolve_approvers(stage: WorkflowStage, instance: WorkflowInstance) -> List[EligibleApprover]:
    """
    Return the eligible approver list for stage on instance at this moment.

    Steps:
      1. Fetch base users holding the permission in the configured scope.
      2. Exclude the requester (C4).
      3. Expand active delegations (C2).
      4. De-duplicate.
    """
    if not stage.approver_permission_key:
        return []

    base_qs = _users_with_permission(
        school=instance.school,
        branch=instance.branch,
        permission_key=stage.approver_permission_key,
        scope=ApproverScope(stage.approver_scope),
    )
    base_qs = base_qs.exclude(pk=instance.requested_by_id)
    base_users = list(base_qs.distinct())
    base_ids = {u.pk for u in base_users}

    now = timezone.now()
    delegations = ApprovalDelegation.objects.filter(
        school=instance.school,
        starts_at__lte=now, ends_at__gte=now,
        revoked_at__isnull=True,
        delegator_id__in=base_ids,
    ).filter(
        Q(document_type="") | Q(document_type=instance.document_type),
    ).exclude(delegate_id=instance.requested_by_id).select_related("delegator", "delegate")

    result: List[EligibleApprover] = []
    seen = set()
    excluded_delegators = {d.delegator_id for d in delegations if d.exclusive}

    for u in base_users:
        if u.pk in excluded_delegators:
            continue
        key = (u.pk, None)
        if key in seen:
            continue
        seen.add(key)
        result.append(EligibleApprover(user=u, on_behalf_of=None))

    for d in delegations:
        key = (d.delegate_id, d.delegator_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(EligibleApprover(user=d.delegate, on_behalf_of=d.delegator))

    return result
