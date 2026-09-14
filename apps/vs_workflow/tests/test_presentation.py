"""The approval-detail contract accepts only generic, display-safe blocks."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from vs_workflow.presentation import (
    InvalidDocumentDetails, changes_section, document_details, fields_section,
    table_section, validate_document_details,
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
