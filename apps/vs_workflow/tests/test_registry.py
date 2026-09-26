"""Tests for handler and condition registries."""
from django.test import SimpleTestCase
from vs_workflow.conditions import register_condition
from vs_workflow.conditions.registry import get_condition_function
from vs_workflow.exceptions import (
    ConditionFunctionAlreadyRegisteredError, HandlerAlreadyRegisteredError,
    UnknownConditionFunctionError, UnknownDocumentTypeError,
)
from vs_workflow.handlers import BaseWorkflowHandler, get_handler, register_handler

class HandlerRegistryTests(SimpleTestCase):
    """Ownership of a document type, one handler per type.

    Every handler below sets ``approval_releases_nothing`` because the registry
    admits none that has not said what an approval of its type releases. These
    ones approve nothing outside the engine, which is that declaration's case.
    """

    def test_register_and_get(self):
        @register_handler("test.docReg")
        class H(BaseWorkflowHandler):
            noun = "Test document"
            approval_releases_nothing = True
            def resolve_default_template_code(self, d): return "x"
        self.assertEqual(get_handler("test.docReg").document_type, "test.docReg")

    def test_duplicate_raises(self):
        @register_handler("test.docDup")
        class H1(BaseWorkflowHandler):
            noun = "Test document"
            approval_releases_nothing = True
            def resolve_default_template_code(self, d): return "x"
        with self.assertRaises(HandlerAlreadyRegisteredError):
            @register_handler("test.docDup")
            class H2(BaseWorkflowHandler):
                noun = "Test document"
                approval_releases_nothing = True
                def resolve_default_template_code(self, d): return "y"

    def test_a_handler_with_no_noun_cannot_register(self):
        """Screens would otherwise show the approver the raw document type."""
        with self.assertRaises(TypeError):
            @register_handler("test.docUnnamed")
            class Unnamed(BaseWorkflowHandler):
                approval_releases_nothing = True
                def resolve_default_template_code(self, d): return "x"

    def test_every_registered_type_has_a_label_of_its_own(self):
        """No live type falls back to its code in words."""
        from vs_workflow.conditions.fields import document_type_label
        from vs_workflow.handlers.registry import list_registered_handlers

        for document_type in list_registered_handlers():
            if document_type.startswith("test."):
                continue
            with self.subTest(document_type=document_type):
                self.assertEqual(
                    document_type_label(document_type),
                    get_handler(document_type).noun,
                )

    def test_unknown_raises(self):
        with self.assertRaises(UnknownDocumentTypeError):
            get_handler("no.such.type")

class ConditionRegistryTests(SimpleTestCase):
    def test_register_and_get(self):
        @register_condition("test.always_trueReg")
        def fn(d, a=None): return True
        self.assertTrue(get_condition_function("test.always_trueReg")(None))

    def test_duplicate_different_raises(self):
        @register_condition("test.dupReg")
        def fn1(d, a=None): return True
        with self.assertRaises(ConditionFunctionAlreadyRegisteredError):
            @register_condition("test.dupReg")
            def fn2(d, a=None): return False

    def test_a_handler_with_no_noun_cannot_register(self):
        """Screens would otherwise show the approver the raw document type."""
        with self.assertRaises(TypeError):
            @register_handler("test.docUnnamed")
            class Unnamed(BaseWorkflowHandler):
                approval_releases_nothing = True
                def resolve_default_template_code(self, d): return "x"

    def test_every_registered_type_has_a_label_of_its_own(self):
        """No live type falls back to its code in words."""
        from vs_workflow.conditions.fields import document_type_label
        from vs_workflow.handlers.registry import list_registered_handlers

        for document_type in list_registered_handlers():
            if document_type.startswith("test."):
                continue
            with self.subTest(document_type=document_type):
                self.assertEqual(
                    document_type_label(document_type),
                    get_handler(document_type).noun,
                )

    def test_unknown_raises(self):
        with self.assertRaises(UnknownConditionFunctionError):
            get_condition_function("never.registered")
