"""The shared judge of an import row's writes asks Field Access about the cells that set a value.

Every dataset that writes a registered resource shares this judge. A blank cell
sets nothing and is never asked about; a dataset whose one column fills several
fields only in part names which ones with ``names_set``. The judge is read here
without a database: Field Access itself is replaced by a recorder, so what is
asserted is which names were put to it and how a refusal is worded.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from vs_import_data.field_writes import (
    import_request,
    write_refusal,
    write_refusal_issue,
)
from vs_rbac.field_enforcement import FieldWriteDenied

WRITES = (("school.guardians", {
    "guardian_full_name": ("first_name", "middle_name", "last_name"),
    "guardian_phone": ("phone",),
}, ""),)


def _asked(payload, **kwargs):
    with mock.patch("vs_import_data.field_writes.assert_writable") as judged:
        refusal = write_refusal(SimpleNamespace(), payload, WRITES, **kwargs)
    names = sorted(judged.call_args.args[2]) if judged.called else []
    return refusal, names


class WriteRefusalTests(SimpleTestCase):

    def test_a_blank_cell_is_never_asked_about(self):
        refusal, names = _asked({"guardian_full_name": "  ", "guardian_phone": None})
        self.assertIsNone(refusal)
        self.assertEqual(names, [])

    def test_every_name_of_a_filled_cell_is_asked_by_default(self):
        _refusal, names = _asked({"guardian_full_name": "Ada Obi", "guardian_phone": "0801"})
        self.assertEqual(names, ["first_name", "last_name", "middle_name", "phone"])

    def test_a_dataset_can_narrow_the_names_a_cell_sets(self):
        def only_first_and_last(target, names, payload):
            return ("first_name", "last_name") if target == "guardian_full_name" else ()

        _refusal, names = _asked(
            {"guardian_full_name": "Ada Obi", "guardian_phone": "0801"},
            names_set=only_first_and_last,
        )
        self.assertEqual(names, ["first_name", "last_name"])

    def test_no_request_refuses_nothing(self):
        self.assertIsNone(write_refusal(None, {"guardian_phone": "0801"}, WRITES))

    def test_a_refusal_names_the_target_field_with_the_registry_label(self):
        denied = FieldWriteDenied(["phone"], labels={"phone": "Phone"})
        with mock.patch("vs_import_data.field_writes.assert_writable", side_effect=denied):
            refusal = write_refusal(SimpleNamespace(), {"guardian_phone": "0801"}, WRITES)

        self.assertEqual(refusal.fields, ["guardian_phone"])
        issue = write_refusal_issue(refusal, "guardian_phone", 4, {"guardian_phone": "Phone"})
        self.assertEqual(issue["metadata"], {"field_write_denied": ["guardian_phone"]})
        self.assertEqual(issue["row_number"], 4)
        self.assertEqual(issue["column_name"], "Phone")
        self.assertIn("Phone", issue["message"])


class ImportRequestTests(SimpleTestCase):

    def test_no_actor_is_the_escape_hatch(self):
        self.assertIsNone(import_request(None, object()))

    def test_the_stand_in_is_kept_on_the_batch_for_the_same_actor_and_tenant(self):
        batch, actor, tenant = SimpleNamespace(), object(), object()
        first = import_request(actor, tenant, batch)

        self.assertIs(import_request(actor, tenant, batch), first)
        self.assertIsNot(import_request(object(), tenant, batch), first)
