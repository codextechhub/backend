"""What this module does when something it owns a record for changes.

``vs_user`` announces an activation and knows nothing about staff records; this
module holds the record and connects to the announcement. That direction is the
point: an engine app must never import anything under ``apps/schools/``.

Both receivers are connected from ``VsStaffConfig.ready()``, which runs after
the app registry is populated, so the models can be imported here and named as
senders rather than filtered inside the handler.
"""
from __future__ import annotations

from django.db.models.signals import post_delete
from django.dispatch import receiver

from vs_user.signals import account_activated

from .models import StaffDocument


@receiver(account_activated, dispatch_uid="vs_staff.promote_on_activation")
def _promote_employment_on_activation(sender, user, **kwargs):
    """Invited becomes Active when the invited person accepts.

    Runs inside the activation transaction, so somebody is never ACTIVE on
    their login and Invited on their record. Does nothing at all for a user with
    no staff profile, which is the ordinary case for a school's first
    administrator and for every account the platform creates for itself.
    """
    from .services.employment import promote_on_activation

    promote_on_activation(user)


@receiver(
    post_delete, sender=StaffDocument, dispatch_uid="vs_staff.delete_document_file",
)
def _delete_document_file(sender, instance, **kwargs):
    """Remove the stored bytes when a staff document row goes.

    The row and the file go together, or a school that removed a passport scan
    still has the scan sitting in storage behind a URL somebody may be holding,
    which is the opposite of what pressing Remove means.

    On the signal rather than in the view, so every path that deletes a document
    loses the file with it: the endpoint, a cascade from the staff record, and a
    management command all reach the same place.
    """
    if instance.file:
        instance.file.delete(save=False)
