"""A document type says what its approval releases, or it does not register.

The engine can withdraw its own record of an approver's vote. It cannot
withdraw what that vote released, so before a reversal writes anything it asks
the module that owns the document. A type that never answers would be reversed
with its effect still standing, and nothing anywhere would say so.

Two things hold that shut, and both are tested here. Registration refuses a
handler that has declared nothing, which is what stops a new document type
shipping silent. The default answer refuses as well, which is what catches a
handler that reaches the engine without passing through the registry. A type
whose approval genuinely leaves nothing behind says so in as many words, and
reverses freely.
"""
from django.test import SimpleTestCase

from vs_workflow.exceptions import (
    ReversalContractNotDeclaredError, ReversalNotAllowedError,
    UnknownDocumentTypeError,
)
from vs_workflow.handlers import (
    BaseWorkflowHandler, get_handler, list_registered_handlers, register_handler,
)
from vs_workflow.handlers.base import declares_reversal_answer


class _Instance:
    """The fields the base handler reads off a workflow instance."""

    def __init__(self, document=None):
        self.document = document
        self.document_object_id = "1"
        self.document_type = "test.reversal"


def _context():
    """The engine's reversal context, as ``reverse_action`` builds it."""
    return {
        "action_id": "act01234", "original_action": "APPROVED",
        "stage_code": "approval", "attempt": 1,
        "reason": "the wrong person was asked",
        "actor_id": "1", "was_final_approval": True,
    }


class UndeclaredTypeTests(SimpleTestCase):
    """A type that has not answered is refused, twice over."""

    def test_a_handler_that_declares_nothing_cannot_register(self):
        """The app that would serve this type fails to load rather than shipping it."""
        with self.assertRaises(ReversalContractNotDeclaredError):
            @register_handler("test.reversalSilent")
            class Silent(BaseWorkflowHandler):
                def resolve_default_template_code(self, document):
                    return "x"

        with self.assertRaises(UnknownDocumentTypeError):
            get_handler("test.reversalSilent")

    def test_the_default_answer_refuses_rather_than_permits(self):
        """The second line, for a handler that never passed the registry.

        A handler reached by patching the lookup, or by an import that writes
        the registry by hand, skips the check at registration. What it cannot
        skip is the answer itself, and the answer inherited from the base is no.
        """
        class Silent(BaseWorkflowHandler):
            document_type = "test.reversalUnregistered"

        with self.assertRaises(ReversalNotAllowedError) as caught:
            Silent().validate_reversal(_Instance(), _context())
        self.assertIn("has not said", str(caught.exception))

    def test_saying_something_is_released_without_saying_what_blocks_is_silence(self):
        """``approval_releases_nothing = False`` answers nothing and is not a declaration."""
        class Silent(BaseWorkflowHandler):
            approval_releases_nothing = False

        self.assertFalse(declares_reversal_answer(Silent))


class DeclaredTypeTests(SimpleTestCase):
    """The three ways a type answers, and what each one does at a reversal."""

    def test_a_type_that_releases_nothing_declares_it_and_reverses(self):
        @register_handler("test.reversalFree")
        class Free(BaseWorkflowHandler):
            """Approval here writes nothing outside the engine."""

            approval_releases_nothing = True

            def resolve_default_template_code(self, document):
                return "x"

        handler = get_handler("test.reversalFree")
        self.assertIsNone(handler.validate_reversal(_Instance(), _context()))

    def test_a_declared_refusal_is_raised_in_the_engine_s_own_shape(self):
        """One hook per type, one exception for the administrator to read."""
        @register_handler("test.reversalGuarded")
        class Guarded(BaseWorkflowHandler):
            def resolve_default_template_code(self, document):
                return "x"

            def reversal_block_reason(self, document):
                return "The goods have already arrived."

        with self.assertRaises(ReversalNotAllowedError) as caught:
            get_handler("test.reversalGuarded").validate_reversal(
                _Instance(), _context(),
            )
        self.assertIn("goods have already arrived", str(caught.exception))
        self.assertEqual(
            caught.exception.extra.get("document_type"), "test.reversalGuarded",
        )

    def test_a_type_that_answers_under_a_lock_declares_by_overriding_the_check(self):
        """A payout batch and a finance document both answer this way.

        Each has to read rows under ``select_for_update`` to answer truthfully,
        so it does the reading and the refusing in one method rather than
        handing the engine a document it would have to lock again.
        """
        @register_handler("test.reversalLocked")
        class Locked(BaseWorkflowHandler):
            def resolve_default_template_code(self, document):
                return "x"

            def validate_reversal(self, instance, context):
                return None

        self.assertIsNone(
            get_handler("test.reversalLocked").validate_reversal(
                _Instance(), _context(),
            ),
        )

    def test_a_module_base_answers_once_for_every_type_it_carries(self):
        """Six finance types and one answer between them, which is the real shape."""
        class _ModuleBase(BaseWorkflowHandler):
            def validate_reversal(self, instance, context):
                return None

        class Concrete(_ModuleBase):
            pass

        self.assertTrue(declares_reversal_answer(Concrete))


class RegisteredTypeTests(SimpleTestCase):
    """Enumerated rather than sampled, so a type added later is caught here."""

    def test_every_registered_document_type_answers_the_reversal_question(self):
        for document_type, handler in list_registered_handlers().items():
            if type(handler).__module__.startswith("vs_workflow.tests"):
                continue
            with self.subTest(document_type=document_type):
                self.assertTrue(
                    declares_reversal_answer(type(handler)),
                    f"{document_type}: its handler must say what approving it "
                    f"releases before a reversal can be allowed",
                )
