"""The parent portal's home, gathered from every module in one read.

Mrs. Nwosu has Chiamaka (Lekki, JSS1 A), Tobi (Ikeja, JSS1 C) and Folu (Epe, no
class yet), so her children sit at three branches. Brightfield has posted a
notice for the whole school and one for Ikeja. When one child's books cannot be
read, that child's fees say so and the others, and everything else on the home,
still arrive.
"""
from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from schools.core.fal import registry as fal_registry
from schools.core.fal.testing import unavailable_finance_reader
from schools.vs_notices.constants import AudienceKind, NoticeStatus
from schools.vs_notices.models import Notice, NoticeAudience

from .fees_base import ParentBooksFixture, UnreadableChildReader

HOME = "parent-portal-home"


class ParentHomeTests(ParentBooksFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sports = cls.notice("Inter-house sports", [{"kind": AudienceKind.SCHOOL}])
        cls.assembly = cls.notice(
            "Ikeja assembly", [{"kind": AudienceKind.BRANCH, "branch": cls.ikeja}],
        )
        cls.science_fair = cls.notice(
            "Lekki science fair",
            [{"kind": AudienceKind.CLASS, "school_class": cls.lekki_class}],
        )

    @classmethod
    def notice(cls, title, audiences):
        notice = Notice.all_objects.create(
            tenant=cls.tenant, title=title, body="Please read.",
            status=NoticeStatus.PUBLISHED, published_at=timezone.now(),
            created_by=cls.admin, published_by=cls.admin,
        )
        for audience in audiences:
            NoticeAudience.objects.create(
                tenant=cls.tenant, notice=notice, kind=audience["kind"],
                branch=audience.get("branch"), school_class=audience.get("school_class"),
            )
        return notice

    def home(self, user=None):
        response = self.read(HOME, user)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_home_gathers_a_block_for_every_child_and_for_the_family(self):
        data = self.home()
        from vs_config.clock import branch_today

        self.assertEqual(data["date"], branch_today(self.tenant, None))
        self.assertEqual(
            [(c["first_name"], c["class_name"]) for c in data["children"]],
            [("Chiamaka", "JSS1 A"), ("Tobi", "JSS1 C"), ("Folu", "")],
        )
        for child in data["children"]:
            for name in ("attendance", "fees", "results", "today"):
                self.assertTrue(child[name]["available"], (child["first_name"], name))

        chiamaka = data["children"][0]
        self.assertEqual(chiamaka["fees"]["balance"], 350_000)
        self.assertEqual(chiamaka["fees"]["status"], "OWING")
        self.assertEqual(chiamaka["fees"]["open_invoice_count"], 2)
        self.assertEqual(data["children"][2]["fees"]["open_invoice_count"], 1)
        self.assertEqual(chiamaka["results"], {"available": True, "released": False})
        self.assertFalse(chiamaka["today"]["published"])
        self.assertIsNone(chiamaka["today"]["next_lesson"])
        self.assertIn("rate", chiamaka["attendance"])

        self.assertEqual(data["fees"], {
            "available": True, "outstanding": 1_000_000, "pay_all_total": 1_000_000,
        })
        self.assertEqual(data["notifications"], {"available": True, "unread_count": 0})

    def test_a_child_with_nothing_owed_is_clear_and_a_credit_is_in_credit(self):
        self.pay(self.books, self.tobi_levy.customer_id, 50_000, invoice=self.tobi_levy)
        self.pay(self.books, self.tobi_tuition.customer_id, 300_000, invoice=self.tobi_tuition)
        tobi = self.home()["children"][1]["fees"]
        self.assertEqual((tobi["balance"], tobi["status"], tobi["open_invoice_count"]), (0, "CLEAR", 0))

    def test_notices_name_the_children_each_is_for_and_count_the_unread(self):
        block = self.home()["notices"]
        self.assertTrue(block["available"])
        self.assertEqual(block["unread_count"], 2)
        latest = block["latest"]
        self.assertEqual(latest["title"], "Ikeja assembly")
        self.assertEqual(latest["children"], ["Tobi"])
        self.assertTrue(latest["unread"])

    def test_a_notice_for_another_familys_class_is_not_in_the_feed(self):
        """Zainab is in JSS1 B and the Nwosu children are not."""
        yusuf = self.home(self.yusuf_user)["notices"]
        self.assertEqual(yusuf["unread_count"], 2)
        self.assertEqual(yusuf["latest"]["title"], "Lekki science fair")
        self.assertEqual(yusuf["latest"]["children"], ["Zainab"])

    def test_opening_a_notice_lowers_the_unread_count(self):
        from schools.vs_notices.services.notices import mark_read

        mark_read(self.assembly, self.nwosu_user)
        block = self.home()["notices"]
        self.assertEqual(block["unread_count"], 1)
        self.assertFalse(block["latest"]["unread"])

    # ── one block failing never fails the home ──────────────────────────

    def test_a_block_that_raises_leaves_the_others_standing(self):
        with mock.patch(
            "schools.vs_student_portal.home.today_block", side_effect=RuntimeError("grid"),
        ), self.assertLogs("schools.vs_parent_portal.home", level="ERROR"):
            data = self.home()
        for child in data["children"]:
            self.assertFalse(child["today"]["available"])
            self.assertNotIn("next_lesson", child["today"])
            self.assertIn(child["first_name"], child["today"]["message"])
            for name in ("attendance", "fees", "results"):
                self.assertTrue(child[name]["available"], name)
        for name in ("fees", "notices", "notifications"):
            self.assertTrue(data[name]["available"], name)

    def test_a_family_block_that_raises_leaves_the_children_standing(self):
        with mock.patch(
            "schools.vs_notices.services.notices.guardian_notices", side_effect=RuntimeError("feed"),
        ), self.assertLogs("schools.vs_parent_portal.home", level="ERROR"):
            data = self.home()
        self.assertEqual(
            data["notices"],
            {"available": False, "message": "Notices cannot be shown right now. Open Notices to try again."},
        )
        self.assertTrue(all(c["fees"]["available"] for c in data["children"]))
        self.assertTrue(data["fees"]["available"])

    def test_books_that_cannot_be_read_are_unavailable_never_zero(self):
        fal_registry.set_finance_reader(unavailable_finance_reader())
        with self.assertLogs("schools.vs_parent_portal.home", level="ERROR"):
            data = self.home()
        for child in data["children"]:
            self.assertEqual(child["fees"], {
                "available": False,
                "message": f"{child['first_name']}'s fees cannot be shown right now. "
                           "Try again in a few minutes.",
            })
            self.assertTrue(child["attendance"]["available"])
        self.assertFalse(data["fees"]["available"])
        self.assertNotIn("outstanding", data["fees"])
        self.assertTrue(data["notices"]["available"])

    def test_one_childs_unreadable_books_leave_the_siblings_fees_standing(self):
        fal_registry.set_finance_reader(UnreadableChildReader(self.tobi.pk))
        with self.assertLogs("schools.vs_parent_portal.home", level="ERROR"):
            data = self.home()
        chiamaka, tobi, folu = data["children"]
        self.assertEqual(chiamaka["fees"]["balance"], 350_000)
        self.assertFalse(tobi["fees"]["available"])
        self.assertEqual(folu["fees"]["balance"], 300_000)
        self.assertFalse(data["fees"]["available"])

    # ── shapes of school ─────────────────────────────────────────────────

    def test_a_parent_at_a_one_branch_school_gets_the_same_home(self):
        data = self.home(self.ade_user)
        self.assertEqual([c["first_name"] for c in data["children"]], ["Bola"])
        self.assertEqual(data["children"][0]["fees"]["status"], "CLEAR")
        self.assertEqual(data["fees"]["pay_all_total"], 0)
        self.assertEqual(data["notices"]["unread_count"], 0)
        self.assertIsNone(data["notices"]["latest"])

    def test_staff_without_a_child_get_404(self):
        self.assertEqual(self.read(HOME, self.admin).status_code, 404)

    def test_each_additional_child_costs_a_bounded_number_of_queries(self):
        """Three children cost about three times one child, not more.

        Mrs. Yusuf has one child and Mrs. Nwosu three; the household-wide
        blocks (notices, notifications, the list of children) are paid once.
        """
        def queries(user):
            self.home(user)  # warm any per-process cache
            with CaptureQueriesContext(connection) as captured:
                self.home(user)
            return len(captured)

        one, three = queries(self.yusuf_user), queries(self.nwosu_user)
        per_child = (three - one) / 2
        self.assertLessEqual(per_child, one, (one, three))
