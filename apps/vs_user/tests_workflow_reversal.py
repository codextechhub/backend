"""What the user-creation handler lets an administrator reverse.

Approving a platform user creation finalises the account and sends its
invitation, and the person on the other end may have opened the link and set a
password before anybody thinks about undoing the approval. The engine can
withdraw its record of the decision; it cannot withdraw the account. So the
handler refuses once the account has moved past waiting for a decision, and
allows it while the request is still sitting on the ladder.
"""
from __future__ import annotations

from django.test import TestCase

from vs_workflow.exceptions import ReversalNotAllowedError


class _Instance:
    """The fields the handler reads off a workflow instance."""

    def __init__(self, user_pk):
        self.document_object_id = str(user_pk)
        self.document_type = "PLATFORM_USER_CREATION"


def _context():
    """The engine's reversal context, as ``reverse_action`` builds it."""
    return {
        "action_id": "act01234", "original_action": "APPROVED",
        "stage_code": "cx-approval", "attempt": 1,
        "reason": "approved against the wrong requisition",
        "actor_id": "1", "was_final_approval": True,
    }


class UserCreationReversalTests(TestCase):

    def setUp(self):
        from django.contrib.auth import get_user_model
        from vs_tenants.models import Tenant
        from vs_user.workflow_handlers import UserCreationWorkflowHandler

        self.User = get_user_model()
        self.tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        self.handler = UserCreationWorkflowHandler()

    def _user(self, status):
        return self.User.objects.create_user(
            email=f"hire-{status.lower()}@test.com", tenant=self.tenant,
            first_name="New", last_name="Hire", status=status,
        )

    def test_a_request_still_awaiting_a_decision_may_be_reversed(self):
        """An earlier stage's vote, with the ladder still running. Nothing has left."""
        user = self._user(self.User.Status.PENDING_APPROVAL)
        self.assertIsNone(self.handler.validate_reversal(_Instance(user.pk), _context()))

    def test_an_invited_account_refuses_the_reversal(self):
        """PENDING is the status finalisation writes: the invitation has gone out."""
        user = self._user(self.User.Status.PENDING)
        with self.assertRaises(ReversalNotAllowedError):
            self.handler.validate_reversal(_Instance(user.pk), _context())

    def test_a_live_account_refuses_the_reversal(self):
        user = self._user(self.User.Status.ACTIVE)
        with self.assertRaises(ReversalNotAllowedError):
            self.handler.validate_reversal(_Instance(user.pk), _context())

    def test_a_deleted_account_is_not_treated_as_a_live_one(self):
        """No row means no account anybody is holding, so there is nothing to protect.

        The same reading the rest of this handler takes: ``on_approved`` and
        ``on_rejected`` both return quietly when the user is gone. The id is the
        one the instance was created against, so it is a real user pk that no
        longer resolves rather than a token no pk could ever be.
        """
        user = self._user(self.User.Status.PENDING_APPROVAL)
        instance = _Instance(user.pk)
        user.delete()
        self.assertIsNone(self.handler.validate_reversal(instance, _context()))
