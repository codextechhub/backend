"""The role grants and permission exceptions a staff profile can be read at.

A grant is revoked rather than deleted, but its reach can be corrected in
place, and a permission exception is replaced by deleting it and creating
another. Neither table alone can say what somebody could do on an earlier day,
so both keep a history, listed on the person's own account.
"""
from vs_history.registry import track

USER = "vs_user.user"


def register():
    """Declare the tracked access models to the history engine."""
    from .models import TenantUserRoleAssignment, UserPermissionOverride

    for model in (TenantUserRoleAssignment, UserPermissionOverride):
        track(model, owners=lambda row: [(USER, row.user_id)])
