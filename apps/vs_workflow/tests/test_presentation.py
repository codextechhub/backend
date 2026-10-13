"""The approval-detail contract accepts only generic, display-safe blocks."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from vs_workflow.presentation import (
    InvalidDocumentDetails, changes_section, document_details, fields_section,
    for_reader, table_section, validate_document_details,
)


class DocumentDetailsContractTests(SimpleTestCase):
    def test_builders_produce_the_versioned_frontend_contract(self):
        payload = document_details(
            fields_section("Request", [("Reason", "Covering leave")]),
            table_section(
                "Items",
                [("name", "Item"), ("amount", "Amount")],
                [{"name": "Textbooks", "amount": "₦25,000.00"}],
            ),
            changes_section("Access", [{
                "operation": "ADD",
                "label": "Approve payouts",
                "restricted": True,
            }]),
        )

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            [section["kind"] for section in payload["sections"]],
            ["fields", "table", "changes"],
        )

    def test_raw_properties_are_refused(self):
        with self.assertRaises(InvalidDocumentDetails):
            validate_document_details({
                "schema_version": 1,
                "sections": [{
                    "kind": "fields",
                    "title": "Unsafe",
                    "items": [],
                    "metadata": {"secret": "raw"},
                }],
            })

    def test_table_rows_must_match_the_declared_columns(self):
        with self.assertRaises(InvalidDocumentDetails):
            validate_document_details({
                "schema_version": 1,
                "sections": [{
                    "kind": "table",
                    "title": "Items",
                    "columns": [{"key": "name", "label": "Name"}],
                    "rows": [{"name": "Books", "internal_id": "42"}],
                }],
            })

    def test_old_instances_may_have_no_layout(self):
        self.assertEqual(validate_document_details({}), {})

    def test_empty_optional_fields_are_omitted(self):
        section = fields_section("Request", [
            ("Role", "Bursar"),
            ("Reason", ""),
            ("Narration", None),
        ])

        self.assertEqual(
            section["items"],
            [{"label": "Role", "value": "Bursar"}],
        )

    def test_platform_user_handler_keeps_contact_data_in_details(self):
        from vs_user.workflow_handlers import UserCreationWorkflowHandler

        document = SimpleNamespace(
            full_name="Ada Okafor", email="ada@example.test", role="Operations",
            status="PENDING_APPROVAL", phone="08000000000",
            get_status_display=lambda: "Pending approval",
        )
        handler = UserCreationWorkflowHandler()

        summary = handler.get_document_summary(document)
        details = handler.get_document_details(document)

        self.assertEqual(summary["fields"], [{"label": "Role", "value": "Operations"}])
        self.assertNotIn("ada@example.test", str(summary))
        self.assertIn("ada@example.test", str(details))


class ColumnsFromSeveralFieldsTests(SimpleTestCase):
    """A value copied from two registered fields needs both of them readable."""

    def layout(self):
        return document_details(
            fields_section("Bank", [("Account", "0123", ("a.one", "b.two")), ("Name", "Ade", "a.one")]),
            table_section(
                "Lines",
                [("account", "Account", ("a.one", "b.two")), ("amount", "Amount")],
                [{"account": "0123", "amount": "10"}],
            ),
        )

    def read_as(self, readable):
        with mock.patch("vs_rbac.field_enforcement.can_read", side_effect=lambda _r, key: key in readable):
            return for_reader(self.layout(), object())

    def test_every_key_is_kept_and_checked(self):
        layout = self.layout()
        column = layout["sections"][1]["columns"][0]
        self.assertEqual((column["access"], column["access_also"]), ("a.one", ["b.two"]))
        self.assertEqual(self.layout(), validate_document_details(layout))

    @staticmethod
    def section(details, kind):
        return next((s for s in details["sections"] if s["kind"] == kind), None)

    def test_a_reader_of_every_key_sees_the_column(self):
        shown = self.read_as({"a.one", "b.two"})
        self.assertEqual([c["key"] for c in self.section(shown, "table")["columns"]],
                         ["account", "amount"])
        self.assertEqual(len(self.section(shown, "fields")["items"]), 2)

    def test_a_reader_missing_either_key_loses_it(self):
        for readable in ({"a.one"}, {"b.two"}):
            with self.subTest(readable=readable):
                shown = self.read_as(readable)
                self.assertEqual([c["key"] for c in self.section(shown, "table")["columns"]],
                                 ["amount"])
        shown = self.read_as({"a.one"})
        self.assertEqual([i["label"] for i in self.section(shown, "fields")["items"]], ["Name"])
        self.assertIsNone(self.section(self.read_as({"b.two"}), "fields"))

    def test_a_further_key_without_a_first_is_refused(self):
        layout = self.layout()
        del layout["sections"][1]["columns"][0]["access"]
        with self.assertRaises(InvalidDocumentDetails):
            validate_document_details(layout)
