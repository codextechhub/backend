"""The JSON type of a reference decides whether it is an id or a code."""
from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from core.references import names_an_id, pick_by_code_or_id


class PickByCodeOrIdTests(SimpleTestCase):
    """The in-memory reader, over rows where one row's code is another row's id."""

    def setUp(self):
        self.target = SimpleNamespace(pk=5100, code="1410")
        self.decoy = SimpleNamespace(pk=7, code="5100")
        self.by_code = {"1410": self.target, "5100": self.decoy}
        self.by_id = {5100: self.target, 7: self.decoy}

    def pick(self, ref, **kwargs):
        return pick_by_code_or_id(ref, self.by_code, self.by_id, **kwargs)

    def test_a_number_is_an_id_even_when_a_code_wears_the_same_digits(self):
        self.assertIs(self.pick(5100), self.target)

    def test_a_string_is_a_code_first(self):
        self.assertIs(self.pick("5100"), self.decoy)
        self.assertIs(self.pick(" 1410 "), self.target)

    def test_a_digit_string_no_code_wears_still_names_an_id(self):
        self.assertIs(self.pick("7"), self.decoy)

    def test_the_code_is_normalised_before_matching(self):
        rows = {"CIKJ": self.target}
        self.assertIs(pick_by_code_or_id("cikj", rows, {}, code=str.upper), self.target)

    def test_unknown_and_blank_references_name_nothing(self):
        self.assertIsNone(self.pick(99))
        self.assertIsNone(self.pick("NOPE"))
        self.assertIsNone(self.pick(None))
        self.assertIsNone(self.pick(""))

    def test_a_boolean_is_not_an_id(self):
        self.assertFalse(names_an_id(True))
        self.assertIsNone(pick_by_code_or_id(True, {}, {1: self.target}))
