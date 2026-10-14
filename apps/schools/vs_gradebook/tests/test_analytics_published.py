"""Analytics over what the real publishing and amendment services leave behind.

The other analytics tests write snapshots by hand (:mod:`.analytics_base`); this
one drives JSS1 A to published through the services and then corrects a mark, so
the shape the fixtures stand in for is the shape publication writes, and the
figures follow the latest version after an amendment.
"""
from __future__ import annotations

from decimal import Decimal

from ..constants import PERM_ANALYTICS_VIEW
from .amend_base import AmendmentFixture
from .base import make_key_role


def mean(values):
    return str((sum(values) / len(values)).quantize(Decimal("0.01")))


class RealSnapshotTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()
        role = make_key_role(cls.tenant, "Analyst", "analyst", (PERM_ANALYTICS_VIEW,))
        cls.analyst = cls.make_user("analyst@brightfield.test", "Ana", "Lyst", role)

    def analytics(self, **query):
        return self.data(self.get(self.analyst, "gradebook-analytics", {"term": self.first_term.pk, **query}))

    def maths_percentages(self):
        return [
            Decimal(self.subject_of(snapshot, "Mathematics")["percentage"])
            for _, snapshot in self.snapshots().values()
        ]

    def test_the_published_class_is_read_with_its_held_student_counted(self):
        body = self.analytics()
        self.assertEqual(body["scope"]["published_count"], 1)
        [maths] = [r for r in body["subjects"] if r["subject"]["name"] == "Mathematics"]
        self.assertEqual(maths["marked"], 4)
        self.assertEqual(maths["average"], mean(self.maths_percentages()))
        self.assertEqual(sum(e["count"] for e in maths["spread"]), 4)

    def test_the_top_of_the_class_is_the_student_the_card_puts_first(self):
        [top] = self.analytics()["tops"]
        self.assertEqual([s["name"] for s in top["students"]], ["Tunde Bello"])
        self.assertEqual(top["average"], "75.00")

    def test_a_class_still_unpublished_adds_nothing(self):
        body = self.analytics()
        self.assertEqual([c["name"] for c in body["scope"]["classes"]], ["JSS1 A"])

    def test_an_approved_amendment_changes_the_figures_to_the_latest_version(self):
        before = self.analytics()
        old = next(r for r in before["subjects"] if r["subject"]["name"] == "Mathematics")["average"]
        self.assertEqual(self.approve_amendment(
            self.approver, self.raised(self.tunde, cells=self.cells(None, None, 20)),
        ).status_code, 200)
        versions = {version for version, _ in self.snapshots().values()}
        self.assertEqual(versions, {1, 2})
        after = self.analytics()
        new = next(r for r in after["subjects"] if r["subject"]["name"] == "Mathematics")["average"]
        self.assertNotEqual(new, old)
        self.assertEqual(new, mean(self.maths_percentages()))
        self.assertEqual(after["scope"]["published_count"], 1)
