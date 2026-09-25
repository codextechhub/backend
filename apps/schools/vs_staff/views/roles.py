"""What a person may do, and where, read on their profile.

This module owns none of it. The grants are ``vs_rbac``'s and are written
through its own endpoints under ``school.roles.assign``; what this view does is
render them beside the record they belong to, with the reach computed rather
than stored so it cannot go stale.

FRD M12 v2.1, FR-005.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_tenants.models import Branch

from ..constants import PERM_OVERRIDES_VIEW, PERM_VIEW
from ..services import posting, roles
from .base import StaffViewMixin


class StaffRolesView(StaffViewMixin, APIView):
    """GET /v1/i/me/staff/<id>/roles/ - grants, reach, history and exceptions.

    The reach is ``visible_branch_ids``, computed on every read. A whole-tenant
    grant reads as School-wide and means every branch; an empty list where
    somebody holds only branch grants that have since been withdrawn is a real
    answer meaning they reach the school-wide rows and nothing else, and must
    not be rendered as though no narrowing applied.

    Revoked grants are shown as history with who revoked them and why, and never
    deleted: "what could this person do before" is the question asked after
    something has gone wrong.

    docstring-name: A staff member's roles and reach
    """

    rbac_permission = PERM_VIEW
    pending_tenant_surface = True

    def get(self, request, pk):
        staff = self.get_staff(pk)
        school_wide, reach_rows = posting.reach_of(staff)
        active = roles.active_grants(staff)
        revoked = roles.revoked_grants(staff)
        branch_ids = set()
        for grant in active + revoked:
            if grant.branch_id is not None:
                branch_ids.add(grant.branch_id)
            elif grant.role_id:
                branch_ids.update(grant.role.branch_ids)
        branch_names = dict(Branch.all_objects.filter(
            tenant=self.tenant, pk__in=branch_ids,
        ).values_list("pk", "name"))

        return success_response(data={
            "roles": [self._grant(grant, branch_names) for grant in active],
            "revoked": [self._revoked(grant, branch_names) for grant in revoked],
            "reach": {
                "school_wide": school_wide,
                "note": (
                    "A school-wide grant reaches every branch."
                    if school_wide
                    else "Reach comes from role grants, not from the posting. "
                         "It can be wider."
                ),
                "branches": [
                    {"id": branch.pk, "name": branch.name, "via": via}
                    for branch, via in reach_rows
                ],
            },
            "overrides": self._overrides(staff),
        })

    def _grant(self, grant, branch_names):
        role = getattr(grant, "role", None)
        role_ids = role.branch_ids if role is not None else []
        effective_ids = role_ids if grant.branch_id is None and role_ids else ([grant.branch_id] if grant.branch_id else [])
        return {
            "id": grant.pk,
            "role": (role.name or role.key) if role else "",
            "role_key": role.key if role else "",
            "school_wide": grant.branch_id is None and not role_ids,
            "branch_id": grant.branch_id,
            "branch_ids": effective_ids,
            "branch_name": ", ".join(branch_names.get(pk, str(pk)) for pk in effective_ids) if effective_ids else "School-wide",
            "granted_at": grant.assigned_at,
            "granted_by": self._actor(grant.assigned_by),
        }

    def _revoked(self, grant, branch_names):
        payload = self._grant(grant, branch_names)
        payload.update({
            "revoked_at": grant.revoked_at,
            "revoked_by": self._actor(grant.revoked_by),
            "reason": getattr(grant, "revoke_reason", "") or "",
        })
        return payload

    def _overrides(self, staff):
        """Shown only to a holder of the override key, and not as an empty block.

        ``school.user_overrides.view`` is CRITICAL and school-admin only,
        deliberately, so that a person cannot learn that exceptions exist on
        their own account. An empty block would say "there are none here", which
        is exactly the fact the restriction exists to withhold, so a caller
        without the key gets the key absent from the payload entirely.
        """
        from vs_rbac.permissions import has_permission

        if not has_permission(
            self.request.user, PERM_OVERRIDES_VIEW, tenant=self.tenant,
        ):
            return None
        return [
            {
                "permission": row.permission.key,
                "mode": row.mode,
                "reason": getattr(row, "reason", "") or "",
                "expires_at": getattr(row, "expires_at", None),
            }
            for row in roles.overrides_for(staff, self.tenant)
        ]

    @staticmethod
    def _actor(user):
        if user is None:
            return None
        return {
            "id": user.pk,
            "name": f"{user.first_name} {user.last_name}".strip(),
        }
