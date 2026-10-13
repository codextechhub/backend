"""The principal's remark templates: seeded on first read, replaced as a whole list."""
from __future__ import annotations

from vs_audit.models import AuditEvent

from ..constants import DEFAULT_REMARK_TEMPLATES
from ..models import RemarkTemplate
from .base import GradebookFixture

DEFAULTS = [
    {"band_min": 70, "text": "An excellent result. Keep it up."},
    {"band_min": 55, "text": "A good result. A little more effort will show."},
    {"band_min": 40, "text": "A fair result. More consistent work is needed."},
    {"band_min": 0, "text": "This result needs attention. Let us meet before next term."},
]


class RemarkTemplateAccessTests(GradebookFixture):
    def test_the_view_key_reads_and_nothing_less_does(self):
        for user in (self.nobody, self.updater, self.eze):
            with self.subTest(user=user.email):
                self.assertEqual(self.get(user, "gradebook-remark-templates").status_code, 403)
        self.assertEqual(self.get(self.viewer, "gradebook-remark-templates").status_code, 200)

    def test_a_write_needs_the_update_key_and_a_school_wide_reach(self):
        body = {"templates": DEFAULTS}
        self.assertEqual(self.put(self.viewer, "gradebook-remark-templates", body).status_code, 403)
        self.assertEqual(self.put(self.nobody, "gradebook-remark-templates", body).status_code, 403)
        response = self.put(self.lekki_admin, "gradebook-remark-templates", body)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "SHARED_RECORD_READ_ONLY")

    def test_each_school_has_its_own_templates(self):
        self.put(self.admin, "gradebook-remark-templates", {"templates": [
            {"band_min": 50, "text": "Keep going."}, {"band_min": 0, "text": "Come and see me."},
        ]})
        theirs = self.data(self.get(self.solo_admin, "gradebook-remark-templates"))
        self.assertEqual(theirs["templates"], DEFAULTS)


class RemarkTemplateTests(GradebookFixture):
    def test_the_first_read_seeds_the_four_defaults_highest_band_first(self):
        self.assertFalse(RemarkTemplate.objects.filter(tenant=self.tenant).exists())
        response = self.get(self.admin, "gradebook-remark-templates")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data(response), {"templates": DEFAULTS})
        self.assertEqual(RemarkTemplate.objects.filter(tenant=self.tenant).count(), 4)

    def test_the_defaults_are_the_constants(self):
        self.assertEqual(
            [(t["band_min"], t["text"]) for t in DEFAULTS], list(DEFAULT_REMARK_TEMPLATES),
        )

    def test_a_second_read_seeds_nothing_more(self):
        self.get(self.admin, "gradebook-remark-templates")
        self.get(self.admin, "gradebook-remark-templates")
        self.assertEqual(RemarkTemplate.objects.filter(tenant=self.tenant).count(), 4)

    def test_the_whole_list_is_replaced(self):
        body = {"templates": [
            {"band_min": 0, "text": "Needs work."},
            {"band_min": 75, "text": "Superb."},
            {"band_min": 50, "text": "Steady."},
        ]}
        response = self.put(self.admin, "gradebook-remark-templates", body)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [(t["band_min"], t["text"]) for t in self.data(response)["templates"]],
            [(75, "Superb."), (50, "Steady."), (0, "Needs work.")],
        )
        self.assertEqual(RemarkTemplate.objects.filter(tenant=self.tenant).count(), 3)
        again = self.data(self.get(self.admin, "gradebook-remark-templates"))
        self.assertEqual(again, self.data(response))

    def test_the_lowest_averages_must_be_different(self):
        response = self.put(self.admin, "gradebook-remark-templates", {"templates": [
            {"band_min": 0, "text": "a"}, {"band_min": 50, "text": "b"}, {"band_min": 50, "text": "c"},
        ]})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "REMARK_BANDS")
        self.assertEqual(
            self.message(response),
            "Two remarks start at 50. Give each remark its own lowest average.",
        )

    def test_one_must_start_at_zero(self):
        response = self.put(self.admin, "gradebook-remark-templates", {"templates": [
            {"band_min": 40, "text": "a"}, {"band_min": 70, "text": "b"},
        ]})
        self.assertEqual(self.code(response), "REMARK_BANDS")
        self.assertIn("starts at 0", self.message(response))
        self.assertEqual(RemarkTemplate.objects.filter(tenant=self.tenant).count(), 0)

    def test_the_list_and_each_row_are_validated(self):
        cases = {
            "empty": [],
            "blank text": [{"band_min": 0, "text": " "}],
            "negative": [{"band_min": -1, "text": "a"}],
            "over": [{"band_min": 101, "text": "a"}],
            "long": [{"band_min": 0, "text": "x" * 301}],
            "too many": [{"band_min": i * 5, "text": "a"} for i in range(11)],
        }
        for label, rows in cases.items():
            with self.subTest(case=label):
                response = self.put(self.admin, "gradebook-remark-templates", {"templates": rows})
                self.assertEqual(response.status_code, 400)

    def test_a_change_is_audited_and_sending_the_same_list_is_not(self):
        self.get(self.admin, "gradebook-remark-templates")
        self.put(self.admin, "gradebook-remark-templates", {"templates": DEFAULTS})
        events = AuditEvent.objects.filter(entity_type="RemarkTemplate")
        self.assertEqual(events.count(), 0)

        changed = [dict(row) for row in DEFAULTS]
        changed[0]["text"] = "Outstanding."
        self.put(self.admin, "gradebook-remark-templates", {"templates": changed})
        event = events.get()
        self.assertEqual((event.module_key, event.tenant), ("GRADEBOOK", self.tenant))
        self.assertEqual(event.metadata["before"]["70"], DEFAULTS[0]["text"])
        self.assertEqual(event.metadata["after"]["70"], "Outstanding.")
