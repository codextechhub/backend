"""A branch bursar reads a central payroll run through their own branch's share.

Corona pays all staff centrally. January's run pays Ada at Ikeja (50,000
gross, 45,000 net), Bola at Lekki (80,000 gross, 72,000 net) and Chidi at Yaba
(60,000 gross, 54,000 net), and posts one journal per branch. Ngozi keeps
Lekki's books, pinned to Lekki. They open January and see Bola's line, Lekki's
share and Lekki's totals, and nothing of Ada's or Chidi's pay, not even summed
into a total. They cannot post, pay or void the run: it is the whole school's,
so each of those is a 403 ``SHARED_RECORD_READ_ONLY`` with nothing changed.

A central run with no Lekki share (nobody at Lekki was paid in it), a central
draft that has no shares yet, and Ikeja's own branch run are all 404 to Ngozi,
as another branch's run always is. Mr Bello, the whole-school bursar, reads
every run whole, as before.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.constants import PayrollRunStatus
from vs_finance.models import JournalEntry, PayrollRun

from .tests_payroll_split import _SplitFixture

REFUSED = "SHARED_RECORD_READ_ONLY"


class _ShareReachFixture(_SplitFixture):

    def setUp(self):
        super().setUp()
        self.chidi = self.salary(self.books, "Chidi Eze", self.yaba, gross=60_000, paye=4_000, pension=2_000)
        self.ngozi = self.client_for(self.tenant, "share-lekki@corona.test", branch=self.lekki)

    def get(self, client, path):
        return client.get(f"/v1/finance/{path}?entity={self.books.code}")

    def runs(self, client):
        response = self.get(client, "payroll-runs/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def summary(self, client):
        response = self.get(client, "payroll-runs/summary/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def assert_nothing_of_ikeja_or_yaba(self, response):
        """Neither name nor any figure of Ada's or Chidi's appears anywhere."""
        text = str(response.data)
        for leaked in ("Ada Obi", "Chidi Eze", "Ikeja", "Yaba", "50000", "45000", "60000",
                       "54000", "190000", "171000"):
            self.assertNotIn(leaked, text)


class ReadingACentralRunThroughTheirShareTests(_ShareReachFixture):

    def test_the_lekki_reader_opens_the_central_run_and_sees_only_lekki(self):
        run = self.posted_run()

        response = self.get(self.ngozi, f"payroll-runs/{run.pk}/")

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertIsNone(data["branch_id"])
        self.assertTrue(data["partial_view"])
        self.assertEqual([line["employee_name"] for line in data["lines"]], ["Bola Lawal"])
        self.assertEqual(
            (data["gross_total"], data["paye_total"], data["pension_total"], data["net_total"]),
            (80_000, 5_000, 3_000, 72_000),
        )
        self.assertEqual([s["branch_name"] for s in data["branch_shares"]], ["Lekki Branch"])
        self.assert_nothing_of_ikeja_or_yaba(response)

    def test_the_runs_list_shows_lekkis_part_only(self):
        self.posted_run()

        response = self.get(self.ngozi, "payroll-runs/")

        self.assertEqual(response.status_code, 200, response.data)
        (row,) = response.data["data"]
        self.assertTrue(row["partial_view"])
        self.assertEqual((row["gross_total"], row["net_total"]), (80_000, 72_000))
        self.assertEqual(len(row["lines"]), 1)
        self.assert_nothing_of_ikeja_or_yaba(response)

    def test_the_summary_counts_only_lekkis_part(self):
        self.posted_run()

        data = self.summary(self.ngozi)

        self.assertEqual(
            (data["runs"], data["employees"], data["net"], data["to_pay"]),
            (1, 1, 72_000, 72_000),
        )

    def test_lekkis_run_status_follows_lekkis_share(self):
        """Paid once Lekki's share is paid, though Ikeja and Yaba are still owed."""
        run = self.posted_run()
        self.act(run, "pay", {"bank_account": self.lekki_bank.pk})

        data = self.get(self.ngozi, f"payroll-runs/{run.pk}/").data["data"]

        self.assertEqual(data["run_status"], PayrollRunStatus.PAID)
        self.assertEqual(self.summary(self.ngozi)["to_pay"], 0)

    def test_a_central_run_booked_wholly_to_lekki_is_lekkis_to_read(self):
        """Posted as one journal, because Bola is the only person it pays."""
        self.ada.delete()
        self.chidi.delete()
        run = self.posted_run()
        self.assertEqual(run.journal.branch_id, self.lekki.pk)

        response = self.get(self.ngozi, f"payroll-runs/{run.pk}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["net_total"], 72_000)
        ikeja_reader = self.client_for(self.tenant, "share-ikeja@corona.test", branch=self.ikeja)
        self.assertEqual(self.get(ikeja_reader, f"payroll-runs/{run.pk}/").status_code, 404)


class WhatStaysOutOfReachTests(_ShareReachFixture):

    def test_a_central_run_with_no_lekki_share_is_not_found(self):
        self.bola.delete()
        run = self.posted_run()

        self.assertEqual(self.get(self.ngozi, f"payroll-runs/{run.pk}/").status_code, 404)
        self.assertEqual(self.runs(self.ngozi), [])
        self.assertEqual(self.summary(self.ngozi)["runs"], 0)
        for action in ("post", "pay", "cancel"):
            with self.subTest(action=action):
                self.assertEqual(self.act(run, action, client=self.ngozi).status_code, 404)

    def test_a_central_draft_is_not_found_until_it_is_shared_out(self):
        run = PayrollRun.objects.get(pk=self.generate().data["data"]["id"])

        self.assertEqual(self.get(self.ngozi, f"payroll-runs/{run.pk}/").status_code, 404)
        self.assertEqual(self.act(run, "post", client=self.ngozi).status_code, 404)
        self.assertEqual(self.runs(self.ngozi), [])

    def test_ikejas_branch_run_is_not_found(self):
        from vs_finance.payroll import generate_run_from_roster

        run = generate_run_from_roster(self.books, pay_date=datetime.date(2026, 1, 25), branch=self.ikeja)

        self.assertEqual(self.get(self.ngozi, f"payroll-runs/{run.pk}/").status_code, 404)
        self.assertEqual(self.runs(self.ngozi), [])
        self.assertEqual(self.summary(self.ngozi)["runs"], 0)


class WritingACentralRunTests(_ShareReachFixture):

    def assert_refused(self, response):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], REFUSED)

    def test_the_lekki_reader_cannot_post_pay_or_void_the_central_run(self):
        run = self.posted_run()
        journals = JournalEntry.objects.filter(entity=self.books).count()

        for action, body in (
            ("post", {}),
            ("pay", {"bank_account": self.lekki_bank.pk}),
            ("pay", {"bank_accounts": [self.lekki_bank.pk]}),
            ("cancel", {}),
        ):
            with self.subTest(action=action, body=body):
                self.assert_refused(self.act(run, action, body, client=self.ngozi))

        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)
        self.assertEqual(
            set(run.branch_shares.values_list("status", flat=True)), {PayrollRunStatus.POSTED},
        )
        self.assertEqual(JournalEntry.objects.filter(entity=self.books).count(), journals)

    def test_a_run_booked_wholly_to_lekki_is_still_the_whole_schools_to_change(self):
        self.ada.delete()
        self.chidi.delete()
        run = self.posted_run()

        for action, body in (("pay", {"bank_account": self.lekki_bank.pk}), ("cancel", {})):
            with self.subTest(action=action):
                self.assert_refused(self.act(run, action, body, client=self.ngozi))
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)

    def test_lekkis_own_branch_run_is_still_theirs_to_post_and_pay(self):
        from vs_finance.payroll import generate_run_from_roster

        run = generate_run_from_roster(self.books, pay_date=datetime.date(2026, 1, 25), branch=self.lekki)

        self.assertEqual(self.act(run, "post", client=self.ngozi).status_code, 200)
        paid = self.act(run, "pay", {"bank_account": self.lekki_bank.pk}, client=self.ngozi)
        self.assertEqual(paid.status_code, 200, paid.data)


class TheWholeSchoolReaderTests(_ShareReachFixture):

    def test_the_whole_school_reader_sees_the_run_whole(self):
        run = self.posted_run()

        data = self.get(self.bello, f"payroll-runs/{run.pk}/").data["data"]

        self.assertFalse(data["partial_view"])
        self.assertEqual(
            sorted(line["employee_name"] for line in data["lines"]),
            ["Ada Obi", "Bola Lawal", "Chidi Eze"],
        )
        self.assertEqual((data["gross_total"], data["net_total"]), (190_000, 171_000))
        self.assertEqual(len(data["branch_shares"]), 3)
        self.assertEqual(self.summary(self.bello)["to_pay"], 171_000)

    def test_the_whole_school_reader_still_posts_pays_and_voids_it(self):
        run = self.posted_run()
        self.assertEqual(self.act(run, "pay", {"bank_account": self.lekki_bank.pk}).status_code, 200)

        # February's run: everybody is already paid for January.
        other = PayrollRun.objects.get(pk=self.generate(pay_date="2026-02-25").data["data"]["id"])
        self.assertEqual(self.act(other, "cancel").status_code, 200)

    def test_a_pinned_reader_at_a_one_branch_school_is_not_narrowed(self):
        self.salary(self.solo_books, "Solo Staff", None, gross=40_000)
        harbour = self.client_for(self.solo_tenant, "share-solo@harbour.test", branch=self.solo_main)
        run = PayrollRun.objects.get(pk=self.generate(harbour, self.solo_books).data["data"]["id"])

        response = harbour.get(f"/v1/finance/payroll-runs/{run.pk}/?entity={self.solo_books.code}")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["partial_view"])
        self.assertEqual(response.data["data"]["gross_total"], 40_000)


class TenantBoundaryTests(_ShareReachFixture):

    def test_another_tenants_reader_cannot_reach_the_run(self):
        run = self.posted_run()
        rival = TenantAPIClient(user=self.grant(
            self.user_for(self.rival_tenant, "share-rival@rival.test"), *self.KEYS,
            tenant=self.rival_tenant, role_key="share-rival",
        ))

        response = rival.get(f"/v1/finance/payroll-runs/{run.pk}/?entity={self.rival_books.code}")

        self.assertEqual(response.status_code, 404, response.data)
