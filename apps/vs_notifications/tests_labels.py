"""Every machine value the notifications API returns travels with words to show.

Both consoles once built their group headings by stripping ``vs_`` off an app
name, so a new app read as "Fenf" and a renamed one changed a heading. The
API now names the area an event belongs to (``source_module_label``), the
settings layer that decided a value (``source_label``), a channel and a
delivery status, and these tests hold it to words rather than code.
"""
from django.test import SimpleTestCase

from vs_config.tests_labels import HumanLabelAssertions

from . import labels
from .constants import EVENT_TYPE_REGISTRY, ChannelChoices, NotificationStatus
from .models import Notification, NotificationTemplate
from .tests import _NotifFixture


class NotificationLabelTablesTests(HumanLabelAssertions, SimpleTestCase):
    def test_every_registered_source_module_has_its_own_label(self):
        """A new event from a new app must arrive with the area's name."""
        modules = {entry["source_module"] for entry in EVENT_TYPE_REGISTRY}
        self.assertEqual(modules - set(labels.SOURCE_MODULE_LABELS), set())

    def test_every_label_and_fallback_reads_as_words(self):
        for table in (
            labels.SOURCE_MODULE_LABELS, labels.SETTING_SOURCE_LABELS,
            labels.CHANNEL_LABELS, labels.STATUS_LABELS,
        ):
            for value, label in table.items():
                self.assertHuman(label, f"for {value!r}")
        self.assertEqual(labels.source_module_label("vs_brand_new"), "Other notifications")
        self.assertHuman(labels.SOURCE_MODULE_FALLBACK)
        self.assertHuman(labels.SETTING_SOURCE_FALLBACK)


class NotificationLabelsAPITests(HumanLabelAssertions, _NotifFixture):
    def test_the_settings_matrix_names_area_channel_and_layer(self):
        response = self._client(self.cx).get("/v1/notify/settings/")

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(
                row["source_module_label"], labels.source_module_label(row["source_module"]),
            )
            self.assertHuman(row["source_module_label"], row["source_module"])
            self.assertHuman(row["channel_label"], row["channel"])
            self.assertHuman(row["source_label"], row["source"])

    def test_the_catalogue_templates_and_new_template_events_name_the_area(self):
        # Free one event's template so the new-template list has a row to name.
        NotificationTemplate.objects.filter(event_type=self._event("ticket.created")).delete()
        client = self._client(self.cx)
        responses = {
            "event-types": client.get("/v1/notify/event-types/"),
            "templates": client.get("/v1/notify/templates/?page_size=100"),
            "available": client.get("/v1/notify/templates/available-events/"),
        }

        for name, response in responses.items():
            self.assertEqual(response.status_code, 200, (name, response.content))
            rows = response.json()["data"]
            self.assertTrue(rows, name)
            for row in rows:
                self.assertHuman(row["source_module_label"], name)
        for row in responses["event-types"].json()["data"]:
            self.assertEqual(len(row["supported_channel_labels"]), len(row["supported_channels"]))
            for label in row["supported_channel_labels"]:
                self.assertHuman(label)

    def test_delivery_history_names_channel_and_status(self):
        failed = Notification.objects.create(
            tenant=self.cx.tenant, recipient=self.cx,
            event_type=self._event("ticket.created"), channel=ChannelChoices.IN_APP,
            body="p", status=NotificationStatus.FAILED,
        )

        response = self._client(self.cx).get("/v1/notify/history/?scope=platform")

        self.assertEqual(response.status_code, 200, response.content)
        row = next(r for r in response.json()["data"] if r["id"] == str(failed.id))
        self.assertEqual((row["status_label"], row["channel_label"]), ("Failed", "In-App"))
