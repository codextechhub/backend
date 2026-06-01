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
    def test_register_and_get(self):
        @register_handler("test.docReg")
        class H(BaseWorkflowHandler):
            def resolve_default_template_code(self, d): return "x"
        self.assertEqual(get_handler("test.docReg").document_type, "test.docReg")

    def test_duplicate_raises(self):
        @register_handler("test.docDup")
        class H1(BaseWorkflowHandler):
            def resolve_default_template_code(self, d): return "x"
        with self.assertRaises(HandlerAlreadyRegisteredError):
            @register_handler("test.docDup")
            class H2(BaseWorkflowHandler):
                def resolve_default_template_code(self, d): return "y"

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

    def test_unknown_raises(self):
        with self.assertRaises(UnknownConditionFunctionError):
            get_condition_function("never.registered")
