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

        return success_response(data={
            "roles": [self._grant(grant) for grant in roles.active_grants(staff)],
            "revoked": [self._revoked(grant) for grant in roles.revoked_grants(staff)],
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

    def _grant(self, grant):
        role = getattr(grant, "role", None)
        return {
            "id": grant.pk,
            "role": (role.name or role.key) if role else "",
            "role_key": role.key if role else "",
            "school_wide": grant.branch_id is None,
            "branch_id": grant.branch_id,
            "branch_name": grant.branch.name if grant.branch_id else "School-wide",
            "granted_at": grant.assigned_at,
            "granted_by": self._actor(grant.assigned_by),
        }

    def _revoked(self, grant):
        payload = self._grant(grant)
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
