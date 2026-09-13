"""What an in-app notification calls itself.

The tray prints a template's subject above its body, and the feed serializer
falls back to the event type's label when that subject is blank. A template
shipping no subject therefore headlines the category and repeats it in the
subscript underneath: "Export ready", over "Quick export is ready", over
"Export ready - Today". Three quick exports in one afternoon arrive as three
identical rows.

These tests hold the whole set to the rule rather than one corner of it: every
in-app template names its own subject, every subject and body survives a render
without a dangling fragment, and the migration that brings existing databases
into line carries exactly the copy the seed defaults carry. The export case is
checked in detail because it is the one where the name has to be derived: a
quick export has no definition to be named after, so it is named by the dataset
it exported.
"""
import importlib
import re
from types import SimpleNamespace

from django.test import SimpleTestCase

from vs_exports.services import _notification_name

from .constants import ChannelChoices, EVENT_TYPE_REGISTRY
from .models import Notification
from .services.render import render_template
from .services.seed import _build_default_templates
from .tests import _NotifFixture


# A migration module name starts with its number, so it cannot be reached by a
# plain import. The copy inside it is what these tests compare.
migration_0018 = importlib.import_module(
    "vs_notifications.migrations.0018_in_app_notification_subjects"
)


_VARIABLE_RE = re.compile(r"\{\{\s*([a-z_0-9]+)")
_IF_RE = re.compile(r"\{%\s*if\s+([a-z_0-9]+)")

#: Event types with no subject of their own to fill: both shipped one already.
_ALREADY_TITLED = {"health.alert_fired", "workflow.final_approved"}


def _in_app_defaults():
    """``{event_key: {"subject": ..., "body": ...}}`` for the in-app channel."""
    return {
        key: value
        for (key, channel), value in _build_default_templates().items()
        if channel == ChannelChoices.IN_APP
    }


def _full_context(*fragments):
    """A value for every variable the given template text references.

    Renders the case the emitters actually produce, where the context is
    complete. The incomplete case is not a blanket rule to test: a subject
    built on a variable its emitter always sends is correct, and only the
    genuinely optional values are guarded, which the tests below check one by
    one against the emitter that sends them.
    """
    names = set()
    for fragment in fragments:
        names |= set(_VARIABLE_RE.findall(fragment))
        names |= set(_IF_RE.findall(fragment))
    return {name: f"value-{name}" for name in names}


class InAppSubjectCopyTests(SimpleTestCase):
    """Every in-app template headlines its own subject, legibly."""

    def test_every_in_app_template_names_a_subject(self):
        blank = [
            key for key, template in _in_app_defaults().items()
            if not template["subject"].strip()
        ]
        self.assertEqual(blank, [], f"in-app templates with no subject: {blank}")

    def test_every_in_app_subject_renders_to_a_clean_line(self):
        for key, template in _in_app_defaults().items():
            with self.subTest(event_key=key):
                rendered = render_template(
                    template["subject"],
                    _full_context(template["subject"]),
                )
                self.assertTrue(rendered.strip(), "rendered to nothing")
                self.assertEqual(rendered, rendered.strip(), "padded with whitespace")
                self.assertNotIn("  ", rendered, "a gap where a value was left out")
                self.assertNotIn(" ,", rendered)
                self.assertNotIn(" .", rendered)

    def test_every_in_app_body_renders_to_a_clean_line(self):
        for key, template in _in_app_defaults().items():
            with self.subTest(event_key=key):
                rendered = render_template(
                    template["body"], _full_context(template["body"]),
                )
                self.assertTrue(rendered.strip(), "rendered to nothing")
                self.assertNotIn("  ", rendered, "a gap where a value was left out")
                self.assertNotIn(" ,", rendered)
                self.assertNotIn(" .", rendered)

    def test_no_subject_is_the_event_label_it_would_have_fallen_back_to(self):
        labels = {row["key"]: row["label"] for row in EVENT_TYPE_REGISTRY}
        for key, template in _in_app_defaults().items():
            with self.subTest(event_key=key):
                self.assertNotEqual(template["subject"].strip(), labels.get(key, ""))

    def test_a_subject_says_more_than_its_body_repeats(self):
        """The headline and the line under it are not the same sentence."""
        for key, template in _in_app_defaults().items():
            with self.subTest(event_key=key):
                subject = render_template(
                    template["subject"], _full_context(template["subject"]),
                ).strip()
                body = render_template(
                    template["body"], _full_context(template["body"]),
                ).strip()
                self.assertNotEqual(subject, body)


class OptionalContextValueTests(SimpleTestCase):
    """The values an emitter really can leave empty are guarded, not printed.

    Each case below names the emitter that sends the empty value, because a
    guard is only justified by one: ``vs_tickets.services.notifications``
    defaults ``actor`` to ``None`` and passes ``actor_name=""`` for every
    ticket event, and ``vs_finance.document_email`` passes
    ``invoice_number=""`` for a receipt that was never allocated to an invoice.
    """

    def _render(self, key, field, context):
        return render_template(_in_app_defaults()[key][field], context)

    def test_a_ticket_with_no_actor_reads_as_a_sentence(self):
        for key in (
            "ticket.assigned", "ticket.status_changed", "ticket.resolved",
            "ticket.closed", "ticket.reopened", "ticket.escalated",
            "ticket.commented", "ticket.attachment_added",
        ):
            with self.subTest(event_key=key):
                template = _in_app_defaults()[key]
                context = _full_context(template["subject"], template["body"])
                context["actor_name"] = ""
                for field in ("subject", "body"):
                    rendered = self._render(key, field, context)
                    self.assertTrue(rendered.strip())
                    self.assertNotIn("  ", rendered)
                    self.assertNotIn(" .", rendered)

    def test_an_unallocated_receipt_names_no_invoice(self):
        template = _in_app_defaults()["billing.payment_received"]
        context = _full_context(template["subject"], template["body"])
        context["invoice_number"] = ""

        body = self._render("billing.payment_received", "body", context)

        self.assertNotIn("applied to", body)
        self.assertNotIn(" ,", body)
        self.assertNotIn("  ", body)

    def test_a_locked_account_with_no_timestamp_still_reads(self):
        template = _in_app_defaults()["user.account_locked"]
        context = _full_context(template["subject"], template["body"])
        context["locked_at"] = ""

        subject = self._render("user.account_locked", "subject", context)

        self.assertEqual(subject, "Your account is locked")


class MigrationCopyTests(SimpleTestCase):
    """The migration and the seed defaults are the same copy, or neither is."""

    def test_the_migration_carries_the_seed_copy_exactly(self):
        defaults = _in_app_defaults()
        for key, definition in migration_0018.TEMPLATES.items():
            with self.subTest(event_key=key):
                self.assertIn(key, defaults)
                self.assertEqual(definition["subject"], defaults[key]["subject"])
                self.assertEqual(definition["body"], defaults[key]["body"])

    def test_the_migration_covers_every_template_that_shipped_blank(self):
        expected = set(_in_app_defaults()) - _ALREADY_TITLED
        self.assertEqual(set(migration_0018.TEMPLATES), expected)

    def test_a_blank_subject_is_filled_and_any_other_is_left_alone(self):
        definition = {
            "subject": "New subject", "body": "New body",
            "previous_body": "Shipped body",
        }

        filled = migration_0018.plan_changes("", "Shipped body", definition)
        kept = migration_0018.plan_changes(
            "A subject somebody typed", "Shipped body", definition,
        )

        self.assertEqual(filled["subject"], "New subject")
        self.assertNotIn("subject", kept)

    def test_a_body_is_replaced_only_while_it_matches_what_it_shipped_with(self):
        definition = {
            "subject": "New subject", "body": "New body",
            "previous_body": "Shipped body",
        }

        untouched = migration_0018.plan_changes("", "Shipped body", definition)
        edited = migration_0018.plan_changes(
            "", "A body somebody rewrote", definition,
        )

        self.assertEqual(untouched["body"], "New body")
        self.assertNotIn("body", edited)


class ExportNotificationNameTests(SimpleTestCase):
    """What an export notification calls the run it announces."""

    def _run(self, *, definition_name=None, dataset_key="", reference="EXP-0001"):
        definition = (
            SimpleNamespace(name=definition_name) if definition_name is not None
            else None
        )
        return SimpleNamespace(
            definition_id=1 if definition is not None else None,
            definition=definition,
            frozen_config={"dataset_key": dataset_key, "name": "Quick export"},
            reference=reference,
        )

    def test_a_quick_export_is_named_by_what_it_exported(self):
        name = _notification_name(
            self._run(dataset_key="procurement.purchase_orders"),
        )

        self.assertEqual(name, "Purchase orders")

    def test_a_saved_export_is_named_by_its_definition(self):
        name = _notification_name(
            self._run(definition_name="Monthly Arrears",
                      dataset_key="procurement.purchase_orders"),
        )

        self.assertEqual(name, "Monthly Arrears")

    def test_a_name_ending_in_export_does_not_gain_a_second_one(self):
        subject = _in_app_defaults()["export.run_completed"]["subject"]

        for stored, expected in (
            ("Vendor export", "Vendor"),
            ("Vendor exports", "Vendor"),
            ("Vendor EXPORT", "Vendor"),
        ):
            with self.subTest(definition=stored):
                name = _notification_name(self._run(definition_name=stored))
                rendered = render_template(subject, {"export_name": name})
                self.assertEqual(rendered, f"{expected} export is ready")

    def test_an_unknown_dataset_falls_back_to_the_reference(self):
        name = _notification_name(
            self._run(dataset_key="nothing.registered", reference="EXP-0042"),
        )

        self.assertEqual(name, "EXP-0042")

    def test_the_subject_names_the_dataset_and_the_body_carries_the_detail(self):
        template = _in_app_defaults()["export.run_completed"]
        context = {
            "export_name": _notification_name(
                self._run(dataset_key="procurement.purchase_orders"),
            ),
            "rows": 40,
            "reference": "EXP-2026-0413",
            "error": "",
        }

        subject = render_template(template["subject"], context)
        body = render_template(template["body"], context)

        self.assertEqual(subject, "Purchase orders export is ready")
        self.assertIn("40 rows", body)
        self.assertIn("EXP-2026-0413", body)
        self.assertNotIn("Quick export", subject + body)

    def test_one_row_is_not_reported_as_rows(self):
        template = _in_app_defaults()["export.run_completed"]

        body = render_template(
            template["body"],
            {"export_name": "Students", "rows": 1, "reference": "EXP-1", "error": ""},
        )

        self.assertIn("1 row.", body)


class ExportNotificationDispatchTests(_NotifFixture):
    """The stored row, not just the template: what the tray will read."""

    def test_the_dispatched_row_headlines_the_dataset(self):
        from .services.dispatch import NotificationService

        NotificationService.send(
            event_key="export.run_completed",
            context={
                "export_name": "Purchase orders",
                "reference": "EXP-2026-0413",
                "rows": 40,
                "error": "",
            },
            recipients=[self.admin_a],
            tenant=self.school_a.tenant,
            metadata={"export_run_id": 7},
        )

        row = Notification.objects.get(
            recipient=self.admin_a, channel=ChannelChoices.IN_APP,
        )
        self.assertEqual(row.subject, "Purchase orders export is ready")
        self.assertIn("40 rows", row.body)
        self.assertEqual(row.event_type.key, "export.run_completed")

