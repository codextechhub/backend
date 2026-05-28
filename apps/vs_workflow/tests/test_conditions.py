"""Tests for the condition evaluator."""
from decimal import Decimal
from django.test import SimpleTestCase
from vs_workflow.conditions import evaluate_condition, register_condition

class _Doc:
    def __init__(self, **kw):
        for k,v in kw.items(): setattr(self,k,v)

class ConditionEvaluatorTests(SimpleTestCase):
    def test_empty(self):
        ok, t = evaluate_condition(None, _Doc())
        self.assertTrue(ok); self.assertEqual(t["kind"], "empty")

    def test_op_gte(self):
        doc = _Doc(amount=Decimal("150000"))
        ok, _ = evaluate_condition({"op":"gte","field":"amount","value":100000}, doc)
        self.assertTrue(ok)
        ok, _ = evaluate_condition({"op":"gte","field":"amount","value":200000}, doc)
        self.assertFalse(ok)

    def test_op_in(self):
        doc = _Doc(category="CAPITAL")
        ok, _ = evaluate_condition({"op":"in","field":"category","value":["CAPITAL","URGENT"]}, doc)
        self.assertTrue(ok)

    def test_all(self):
        doc = _Doc(amount=Decimal("150000"), category="CAPITAL")
        ok, t = evaluate_condition({"all":[{"op":"gte","field":"amount","value":100000},
                                           {"op":"eq","field":"category","value":"CAPITAL"}]}, doc)
        self.assertTrue(ok); self.assertEqual(t["kind"], "all")

    def test_any(self):
        doc = _Doc(amount=Decimal("50000"), category="CAPITAL")
        ok, _ = evaluate_condition({"any":[{"op":"gte","field":"amount","value":100000},
                                           {"op":"eq","field":"category","value":"CAPITAL"}]}, doc)
        self.assertTrue(ok)

    def test_not(self):
        doc = _Doc(category="REGULAR")
        ok, _ = evaluate_condition({"not":{"op":"eq","field":"category","value":"CAPITAL"}}, doc)
        self.assertTrue(ok)

    def test_named_function(self):
        @register_condition("test.is_vip_tc")
        def _fn(document, args=None): return getattr(document,"category","")=="VIP"
        ok, t = evaluate_condition({"fn":"test.is_vip_tc"}, _Doc(category="VIP"))
        self.assertTrue(ok); self.assertEqual(t["kind"], "fn")

    def test_named_function_error_returns_false(self):
        @register_condition("test.boom_tc")
        def _fn(document, args=None): raise RuntimeError("boom")
        ok, t = evaluate_condition({"fn":"test.boom_tc"}, _Doc())
        self.assertFalse(ok); self.assertIn("error", t)
