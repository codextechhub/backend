"""Giving guardians their own sign-in account for the parent portal.

Reading where a guardian's access stands needs only the student view key, since
it is part of the record. Giving, resending and taking away access need
``school.portal_access.issue``, because each creates or closes a login.

A guardian carries no branch, so every route reaches the guardian through the
caller's school: another school's guardian answers 404. A caller bound to
branches also needs to reach at least one of the guardian's children before
they can give, resend or take away access, so a branch head cannot open or
close the login of a parent whose children are all at another branch.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.field_enforcement import can_read
from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

from ..constants import PERM_PORTAL_ACCESS, PERM_VIEW
from ..services import guardian_portal_access as access
from ..services import guardians as guardian_service
from .base import StudentsViewMixin

RESOURCE = "school.guardians"


def access_block(request, guardian) -> dict:
    """The access block, as far as the caller's Field Access reaches.

    The block is one switch (``school.guardians.portal_access``): a caller who
    may not read it gets an empty block. The email inside it is the guardian's
    email under another name, so it follows its own switch and cannot
    reappear here when hidden on the record.
    """
    if not can_read(request, f"{RESOURCE}.portal_access"):
        return {}
    block = access.describe(guardian)
    if not can_read(request, f"{RESOURCE}.email"):
        block["sign_in_identifiers"].pop("email")
    return block


class _GuardianAccessView(StudentsViewMixin, APIView):
    """What the portal-access routes of a guardian share."""

    def reachable_guardian(self, pk):
        """The guardian, for a caller who may act on their login.

        A caller bound to branches must reach at least one of the guardian's
        children; the refusal is a 404, as for a guardian of another school.
        """
        guardian = self.guardian(pk)
        user = self.request.user
        if visible_branch_ids(user, self.tenant) is not WHOLE_TENANT and not (
            guardian_service.wards_queryset(guardian, user, self.tenant).exists()
        ):
            raise NotFound("No such guardian at this school.")
        return guardian


class GuardianPortalAccessView(_GuardianAccessView):
    """GET, POST, DELETE /v1/guardians/<id>/portal-access/

    GET says whether the guardian has an account for the parent portal and
    where it stands. POST gives them one and sends the invitation to their own
    email; a guardian who already has a working account, such as a teacher
    whose own child attends, is answered as active and nothing is created.
    DELETE takes access away: an account made only for the parent portal is
    closed and every session it holds ends, while an account also used for
    anything else is left working and only unlinked from the guardian.

    docstring-name: A guardian's portal access
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_VIEW if self.request.method == "GET" else PERM_PORTAL_ACCESS
        )
        return super().get_permissions()

    def get(self, request, pk):
        return success_response(data=access_block(request, self.guardian(pk)))

    def post(self, request, pk):
        guardian = self.reachable_guardian(pk)
        name = self.name_of(guardian, RESOURCE)
        already = access.state_of(guardian) == access.ACTIVE
        guardian = access.issue(guardian, actor=request.user)
        if already:
            return success_response(
                f"{name} already has an account and can open the parent portal.",
                data=access_block(request, guardian),
            )
        return success_response(
            f"{name} has been given portal access. The invitation is on its way.",
            data=access_block(request, guardian), status=201,
        )

    def delete(self, request, pk):
        guardian = self.reachable_guardian(pk)
        name = self.name_of(guardian, RESOURCE)
        guardian, account_closed = access.revoke(guardian, actor=request.user)
        message = (
            f"{name}'s parent portal account has been closed."
            if account_closed else
            f"{name}'s parent portal access has been taken away. Their account "
            f"is also used for something else, so it was left working."
        )
        return success_response(message, data=access_block(request, guardian))


class GuardianPortalAccessReissueView(_GuardianAccessView):
    """POST /v1/guardians/<id>/portal-access/reissue/

    A fresh invitation link for a guardian who has not activated yet. The old
    link stops working.

    docstring-name: Resend a guardian's portal invitation
    """

    def get_permissions(self):
        self.rbac_permission = PERM_PORTAL_ACCESS
        return super().get_permissions()

    def post(self, request, pk):
        guardian = access.reissue(self.reachable_guardian(pk), actor=request.user)
        return success_response(
            f"{self.name_of(guardian, RESOURCE)}'s invitation has been sent again.",
            data=access_block(request, guardian),
        )
