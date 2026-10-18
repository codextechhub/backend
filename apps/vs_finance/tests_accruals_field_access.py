"""Field Access on the receivables accruals: deferred income, provisions, deposits and policy.

Corona runs Ikeja and Lekki; Harbour runs one branch. Each class builds one role
per access state in ``setUpTestData`` (a role that cannot read a field, one that
can read and not write it, one that can write what it cannot read) and drives the
real endpoints as those users, so a rule is proved on the route that serves it
and on every other route that carries the same value.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest import mock

from rest_framework.test import APIRequestFactory

from core.test_utils import TenantAPIClient
from vs_finance.constants import ChargeKind, DepositStatus
from vs_finance.models import (
    CreditNote,
    CustomerDeposit,
    DeferredIncomeRelease,
    DoubtfulDebtProvision,
    FinanceReceivablesPolicy,
)
from vs_finance.tests_accruals import D, _AccrualFixture
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_rbac.field_enforcement import field_access_payload
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

DI = "finance.deferredincome"
PROVISION = "finance.provision"
DEPOSIT = "finance.deposit"
SETTINGS = "finance.settings"

ACCRUAL_KEYS = (
    "finance.deferredincome.view", "finance.deferredincome.run", "finance.deferredincome.reverse",
    "finance.provision.view", "finance.provision.create", "finance.provision.submit",
    "finance.provision.post",
    "finance.deposit.view", "finance.deposit.settle", "finance.deposit.run",
    "finance.settings.view", "finance.settings.update",
    "finance.invoice.view", "finance.audit.view",
)


class _AccrualsFieldAccess(_AccrualFixture):
    """The accrual books, plus helpers that mint a user for an access state."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.field_keys = install_declared_fields(DI, PROVISION, DEPOSIT, SETTINGS)

    @classmethod
    def staff(cls, tenant, name, *, hide=(), read_only=(), branch=None):
        """A user holding every accrual permission, with the named field states.

        ``hide`` closes Read and Write, and ``read_only`` leaves Read and closes
        Write. A write without a read is not a state a role can hold.
        """
        user = _FinanceBranchFixture.grant(
            _FinanceBranchFixture.user_for(tenant, f"{name}@accruals-fa.test"),
            *ACCRUAL_KEYS, tenant=tenant, role_key=name, branch=branch,
        )
        role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
        set_field_access(role, *cls.field_keys, read=True, write=True)
        if hide:
            set_field_access(role, *hide, read=False, write=False)
        if read_only:
            set_field_access(role, *read_only, read=True, write=False)
        return user

    def url(self, path, books=None):
        return f"/v1/finance/{path}?entity={(books or self.books).code}"

    def api(self, user):
        return TenantAPIClient(user=user)

    def deposit_bill(self, customer, amount=50_000, *, on=D(2026, 1, 10)):
        return self.bill(customer, amount, on=on, service=None, kind=ChargeKind.DEPOSIT)

    def code(self, response):
        return response.data["error"]["code"]


def _stub_release(**overrides):
    """A release as the list view annotates it, for the sentence that explains an undo."""
    values = dict(
        reversed_at=None, period_id=3, period_start=D(2027, 1, 1), period_end=D(2027, 1, 31),
        period_name="2027-01", period_status="OPEN", branch_period_status="OPEN",
        branch=SimpleNamespace(name="Lekki Branch"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------- #
# Deferred income                                                             #
# --------------------------------------------------------------------------- #

class DeferredIncomeFieldAccessTests(_AccrualsFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        k = f"{DI}."
        cls.open = cls.staff(cls.tenant, "di-open")
        cls.no_pending = cls.staff(cls.tenant, "di-no-pending", hide=[k + "pending_balance"])
        cls.no_monthly = cls.staff(cls.tenant, "di-no-monthly", hide=[k + "monthly_releases"])
        cls.no_row_values = cls.staff(cls.tenant, "di-no-row-values", hide=[
            k + "release_amount", k + "journal", k + "reversal_state",
            k + "reverse_blocked_reason",
        ])
        cls.read_only = cls.staff(cls.tenant, "di-read-only",
                                  read_only=[k + "release_date", k + "period"])
        cls.no_period = cls.staff(cls.tenant, "di-no-period", hide=[k + "period"])
        cls.no_release_date = cls.staff(cls.tenant, "di-no-release-date",
                                        hide=[k + "release_date"])
        cls.no_reversal = cls.staff(cls.tenant, "di-no-reversal", hide=[k + "reversal_state"])
        cls.solo_open = cls.staff(cls.solo_tenant, "di-solo-open")
        cls.lekki_pinned = cls.staff(cls.tenant, "di-lekki", branch=cls.lekki,
                                     hide=[k + "pending_balance"])

    def released(self, *, up_to=D(2027, 1, 31)):
        from vs_finance.deferred_income import release_deferred_income

        self.bill(self.eze)
        return release_deferred_income(self.books, up_to=up_to)

    # -- reads ---------------------------------------------------------------- #

    def test_a_hidden_waiting_balance_leaves_the_other_figures_in_place(self):
        self.bill(self.eze)
        data = self.api(self.no_pending).get(self.url("deferred-income/")).data["data"]
        self.assertNotIn("pending", data)
        self.assertEqual(sorted(data), ["by_month", "released"])
        full = self.api(self.open).get(self.url("deferred-income/")).data["data"]
        self.assertEqual(full["pending"], 150_000)

    def test_hidden_monthly_releases_leave_neither_the_list_nor_its_rows(self):
        self.bill(self.eze)
        data = self.api(self.no_monthly).get(self.url("deferred-income/")).data["data"]
        self.assertEqual(sorted(data), ["pending", "released"])

    def test_the_release_list_drops_every_hidden_row_value(self):
        (release, *_rest) = self.released()
        rows = self.api(self.no_row_values).get(self.url("deferred-income/releases/")).data["data"]
        self.assertTrue(rows)
        for row in rows:
            for name in ("amount", "journal_id", "journal_number",
                         "reversed", "reversed_at", "can_reverse", "reverse_blocked_reason"):
                self.assertNotIn(name, row)
            self.assertIn("branch_id", row)
            self.assertIn("branch_name", row)
            self.assertIn("date", row)
            self.assertIn("period_label", row)
        full = self.api(self.open).get(self.url("deferred-income/releases/")).data["data"][0]
        self.assertEqual(full["branch_name"], "Ikeja Branch")
        self.assertEqual(full["amount"], 37_500)
        self.assertTrue(full["can_reverse"])

    def test_a_filter_on_a_hidden_field_is_refused_not_answered(self):
        self.released()
        releases = self.url("deferred-income/releases/")
        cases = (
            (self.no_row_values, releases + "&reversed=true", "Reversal state"),
            (self.no_period, releases + "&period=1", "Accounting period"),
            (self.no_release_date, releases + "&month=2027-01", "Release date"),
        )
        for user, path, label in cases:
            with self.subTest(path=path):
                response = self.api(user).get(path)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_read_denied")
                self.assertIn(label, response.data["message"])

    def test_the_reason_an_undo_is_blocked_names_a_month_only_to_a_reader(self):
        from vs_finance.views_accruals import _reverse_blocked_reason

        def request_of(user):
            request = APIRequestFactory().get("/")
            request.tenant = self.tenant
            request.user = user
            return request

        closed_branch = _stub_release(branch_period_status="CLOSED")
        sealed = _stub_release()
        closed_month = _stub_release(period_status="CLOSED")

        request = request_of(self.open)
        self.assertEqual(
            _reverse_blocked_reason(closed_branch, [], self.tenant, request),
            "Lekki Branch has closed January 2027; its release cannot be undone while "
            "that month is closed.",
        )
        self.assertIn("Lekki Branch and Ikeja Branch have closed January 2027",
                      _reverse_blocked_reason(sealed, ["Lekki Branch", "Ikeja Branch"],
                                              self.tenant, request))
        self.assertIn("January 2027 is closed",
                      _reverse_blocked_reason(closed_month, [], self.tenant, request))

        request = request_of(self.no_period)
        for release, sealed_by in ((closed_branch, []), (sealed, ["Lekki Branch", "Ikeja Branch"]),
                                   (closed_month, [])):
            said = _reverse_blocked_reason(release, sealed_by, self.tenant, request)
            for name in ("January", "2027"):
                self.assertNotIn(name, said)
        self.assertEqual(
            _reverse_blocked_reason(closed_branch, [], self.tenant, request),
            "Lekki Branch has closed the month; its release cannot be undone while that "
            "month is closed.",
        )
        self.assertEqual(
            _reverse_blocked_reason(closed_month, [], self.tenant, request),
            "The month is not open; its releases cannot be undone while the month is closed.",
        )
        self.assertEqual(
            _reverse_blocked_reason(sealed, ["Lekki Branch"], self.tenant, request),
            "Lekki Branch has closed the month, and the undo reverses every branch's release "
            "of the month together.",
        )

        request = request_of(self.no_row_values)
        self.assertIn("Lekki Branch has closed January 2027",
                      _reverse_blocked_reason(closed_branch, [], self.tenant, request))

    def test_the_invoice_detail_block_follows_the_deferred_income_switches(self):
        invoice = self.bill(self.eze)
        path = f"invoices/{invoice.pk}/"
        full = self.api(self.open).get(self.url(path)).data["data"]["deferred_income"]
        self.assertEqual(sorted(full), ["pending", "released"])
        hidden = self.api(self.no_pending).get(self.url(path)).data["data"]["deferred_income"]
        self.assertEqual(sorted(hidden), ["released"])

    # -- writes --------------------------------------------------------------- #

    def test_a_read_only_release_date_refuses_up_to_and_releases_nothing(self):
        self.bill(self.eze, on=D(2026, 1, 5), service=(D(2026, 2, 2), D(2026, 4, 30)))
        with mock.patch("vs_finance.views_accruals.tenant_today", return_value=D(2026, 6, 1)):
            refused = self.api(self.read_only).post(
                self.url("deferred-income/release/"), {"up_to": "2026-04-30"}, format="json")
            self.assertEqual(refused.status_code, 403, refused.data)
            self.assertEqual(self.code(refused), "field_write_denied")
            self.assertIn("Release date", refused.data["message"])
            self.assertFalse(DeferredIncomeRelease.objects.exists())

            blank = self.api(self.read_only).post(
                self.url("deferred-income/release/"), {"up_to": ""}, format="json")
            self.assertEqual(blank.status_code, 200, blank.data)

    def test_a_hidden_release_date_is_neither_accepted_nor_echoed(self):
        self.bill(self.eze, on=D(2026, 1, 5), service=(D(2026, 2, 2), D(2026, 4, 30)))
        with mock.patch("vs_finance.views_accruals.tenant_today", return_value=D(2026, 6, 1)):
            refused = self.api(self.no_release_date).post(
                self.url("deferred-income/release/"), {"up_to": "2026-03-31"}, format="json")
            self.assertEqual(refused.status_code, 403, refused.data)
            self.assertFalse(DeferredIncomeRelease.objects.exists())

            response = self.api(self.no_release_date).post(
                self.url("deferred-income/release/"), {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertNotIn("up_to", data)
        self.assertEqual(len(data["releases"]), 3)
        for row in data["releases"]:
            self.assertNotIn("date", row)
            self.assertIn("amount", row)

    def test_an_open_release_run_names_the_day_and_each_release(self):
        self.bill(self.eze, on=D(2026, 1, 5), service=(D(2026, 2, 2), D(2026, 4, 30)))
        with mock.patch("vs_finance.views_accruals.tenant_today", return_value=D(2026, 6, 1)):
            response = self.api(self.open).post(
                self.url("deferred-income/release/"), {"up_to": "2026-03-31"}, format="json")
        data = response.data["data"]
        self.assertEqual(data["up_to"], "2026-03-31")
        self.assertEqual(sorted(data["releases"][0]),
                         ["amount", "branch_id", "date", "id", "journal_id"])

    def test_a_read_only_period_refuses_the_undo_and_reverses_nothing(self):
        (release, *_rest) = self.released()
        period = self.period(2027, 1)
        refused = self.api(self.read_only).post(
            self.url("deferred-income/reverse/"), {"period": period.pk}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.code(refused), "field_write_denied")
        self.assertIn("Accounting period", refused.data["message"])
        release.refresh_from_db()
        self.assertIsNone(release.reversed_at)

    def test_a_hidden_period_cannot_be_named_to_undo_it(self):
        (release, *_rest) = self.released()
        period = self.period(2027, 1)
        refused = self.api(self.no_period).post(
            self.url("deferred-income/reverse/"), {"period": period.pk}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.code(refused), "field_write_denied")
        release.refresh_from_db()
        self.assertIsNone(release.reversed_at)

    def test_a_hidden_reversal_state_hides_the_count_and_an_open_caller_reads_both(self):
        self.released()
        period = self.period(2027, 1)
        hidden = self.api(self.no_reversal).post(
            self.url("deferred-income/reverse/"), {"period": period.pk}, format="json")
        self.assertEqual(hidden.data["data"], {"period": period.pk})

    def test_an_open_undo_names_the_period_and_the_count(self):
        self.released()
        period = self.period(2027, 1)
        response = self.api(self.open).post(
            self.url("deferred-income/reverse/"), {"period": period.pk}, format="json")
        self.assertEqual(response.data["data"], {"period": period.pk, "reversed": 1})

    def test_a_branch_bound_caller_cannot_run_or_undo_for_every_branch(self):
        client = self.api(self.lekki_pinned)
        self.assertEqual(client.post(
            self.url("deferred-income/release/"), {}, format="json").status_code, 403)
        self.assertEqual(client.post(
            self.url("deferred-income/reverse/"), {"period": self.period(2027, 1).pk},
            format="json").status_code, 403)

    # -- the access map ------------------------------------------------------- #

    def test_the_access_map_lists_the_period_body_key_as_read_only_when_it_cannot_be_written(self):
        mapped = field_access_payload(self.read_only, self.tenant)[DI]
        self.assertIn("period", mapped["read_only"])
        self.assertIn("up_to", mapped["read_only"])
        self.assertEqual(mapped["hidden"], [])
        closed = field_access_payload(self.no_period, self.tenant)[DI]
        self.assertIn("period", closed["hidden"])
        self.assertIn("period_label", closed["hidden"])

    # -- single branch, other tenant ------------------------------------------ #

    def test_a_single_branch_school_always_shows_its_branch(self):
        from vs_finance.deferred_income import release_deferred_income

        self.bill(self.ada)
        release_deferred_income(self.solo_books, up_to=D(2027, 1, 31))
        path = self.url("deferred-income/releases/", self.solo_books)
        shown = self.api(self.solo_open).get(path).data["data"]
        self.assertEqual([row["branch_id"] for row in shown], [self.solo_main.pk])

    def test_closing_a_field_in_one_school_leaves_the_other_open_and_serves_no_foreign_row(self):
        self.bill(self.eze)
        self.bill(self.ada)
        corona = self.api(self.no_pending).get(self.url("deferred-income/")).data["data"]
        harbour = self.api(self.solo_open).get(
            self.url("deferred-income/", self.solo_books)).data["data"]
        self.assertNotIn("pending", corona)
        self.assertEqual(harbour["pending"], 150_000)
        self.assertEqual(self.api(self.solo_open).get(
            self.url("deferred-income/releases/", self.books)).status_code, 404)


# --------------------------------------------------------------------------- #
# Doubtful-debt provisions                                                    #
# --------------------------------------------------------------------------- #

class ProvisionFieldAccessTests(_AccrualsFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        k = f"{PROVISION}."
        cls.open = cls.staff(cls.tenant, "pv-open")
        cls.no_status = cls.staff(cls.tenant, "pv-no-status", hide=[k + "status"])
        line_values = [k + "required", k + "current", k + "movement", k + "bands", k + "journal"]
        cls.no_line_values = cls.staff(cls.tenant, "pv-no-line-values", hide=line_values)
        cls.lekki_no_line_values = cls.staff(
            cls.tenant, "pv-lekki-no-line-values", branch=cls.lekki, hide=line_values)
        cls.no_totals = cls.staff(cls.tenant, "pv-no-totals",
                                  hide=[k + "required_total", k + "movement_total",
                                        k + "policy_snapshot", k + "created_at"])
        cls.read_only = cls.staff(cls.tenant, "pv-read-only",
                                  read_only=[k + "as_of", k + "narration"])
        cls.narration_read_only = cls.staff(cls.tenant, "pv-narration-ro",
                                            read_only=[k + "narration"])
        cls.lekki_no_required_total = cls.staff(cls.tenant, "pv-lekki-no-total", branch=cls.lekki,
                                                hide=[k + "required_total"])
        cls.lekki_open = cls.staff(cls.tenant, "pv-lekki-open", branch=cls.lekki)
        cls.solo_open = cls.staff(cls.solo_tenant, "pv-solo-open")
        cls.no_date = cls.staff(cls.tenant, "pv-no-date", hide=[k + "as_of"])

    def run_of(self, books=None):
        from vs_finance.provisions import prepare_provision

        books = books or self.books
        for customer in (self.eze, self.bello) if books == self.books else (self.ada,):
            self.bill(customer, 1_000_000, on=D(2026, 1, 5), due=D(2026, 1, 10), service=None)
        return prepare_provision(books, as_of=D(2026, 12, 31), narration="Year end")

    # -- reads ---------------------------------------------------------------- #

    def test_a_period_is_refused_where_the_provision_date_is_hidden(self):
        self.run_of()
        for query in ("&fiscal_year=2026", "&date_from=2026-12-01"):
            with self.subTest(query=query):
                refused = self.api(self.no_date).get(self.url("provisions/") + query)
                self.assertEqual(refused.status_code, 403, refused.data)
                self.assertEqual(self.code(refused), "field_read_denied")
                self.assertIn("Provision date", refused.data["message"])
        self.assertEqual(self.api(self.no_date).get(self.url("provisions/")).status_code, 200)

    def test_a_period_narrows_runs_for_a_reader_of_the_provision_date(self):
        self.run_of()
        for user in (self.open, self.read_only):
            with self.subTest(user=user.email):
                path = self.url("provisions/")
                self.assertEqual(
                    len(self.api(user).get(path + "&date_from=2026-12-31").data["data"]), 1)
                self.assertEqual(
                    len(self.api(user).get(path + "&date_from=2027-01-01").data["data"]), 0)

    def test_a_hidden_status_leaves_the_list_and_the_detail_and_the_number_stays(self):
        run = self.run_of()
        for path in ("provisions/", f"provisions/{run.pk}/"):
            with self.subTest(path=path):
                data = self.api(self.no_status).get(self.url(path)).data["data"]
                row = data[0] if isinstance(data, list) else data
                for name in ("status", "approval_state", "approval_returned",
                             "approval_required"):
                    self.assertNotIn(name, row)
                self.assertEqual(row["document_number"], run.document_number)
                self.assertEqual(row["narration"], "Year end")
                self.assertEqual(len(row["lines"]), 2)

    def test_hidden_totals_snapshot_and_raised_time_leave_both_reads(self):
        run = self.run_of()
        detail = self.api(self.no_totals).get(self.url(f"provisions/{run.pk}/")).data["data"]
        for name in ("required_total", "movement_total", "policy_snapshot", "created_at"):
            self.assertNotIn(name, detail)
        self.assertIn("as_of", detail)

    def test_hidden_line_fields_leave_each_line_with_only_its_branch(self):
        run = self.run_of()
        detail = self.api(self.no_line_values).get(self.url(f"provisions/{run.pk}/")).data["data"]
        self.assertEqual(len(detail["lines"]), 2)
        listed = self.api(self.no_line_values).get(self.url("provisions/")).data["data"][0]
        for line in (*detail["lines"], *listed["lines"]):
            self.assertEqual(sorted(line), ["branch_id", "branch_name"])

    def test_a_branch_bound_reader_who_may_read_no_line_figure_still_gets_only_their_part(self):
        run = self.run_of()
        detail = self.api(self.lekki_no_line_values).get(self.url(f"provisions/{run.pk}/"))
        self.assertEqual(detail.status_code, 200, detail.data)
        data = detail.data["data"]
        self.assertTrue(data["partial_view"])
        self.assertEqual(
            data["lines"], [{"branch_id": self.lekki.pk, "branch_name": "Lekki Branch"}])
        lekki_line = run.lines.get(branch=self.lekki)
        self.assertEqual(data["required_total"], lekki_line.required)

    def test_a_partial_view_does_not_bring_back_a_total_the_caller_may_not_read(self):
        run = self.run_of()
        detail = self.api(self.lekki_no_required_total).get(
            self.url(f"provisions/{run.pk}/")).data["data"]
        self.assertTrue(detail["partial_view"])
        self.assertNotIn("required_total", detail)
        self.assertIn("movement_total", detail)
        listed = self.api(self.lekki_no_required_total).get(self.url("provisions/")).data["data"][0]
        self.assertNotIn("required_total", listed)

    def test_a_branch_part_for_a_reader_who_may_read_everything_is_unchanged(self):
        run = self.run_of()
        data = self.api(self.lekki_open).get(self.url(f"provisions/{run.pk}/")).data["data"]
        lekki_line = run.lines.get(branch=self.lekki)
        self.assertEqual([row["branch_id"] for row in data["lines"]], [self.lekki.pk])
        self.assertEqual(data["required_total"], lekki_line.required)
        self.assertIsNone(data["approval_required"])

    def test_a_filter_on_a_hidden_field_is_refused(self):
        self.run_of()
        by_approval = self.api(self.no_status).get(
            self.url("provisions/") + "&approval=returned")
        self.assertEqual(by_approval.status_code, 403, by_approval.data)
        self.assertEqual(self.code(by_approval), "field_read_denied")

    # -- writes --------------------------------------------------------------- #

    def test_a_read_only_provision_date_refuses_a_new_run_and_saves_nothing(self):
        before = DoubtfulDebtProvision.objects.count()
        refused = self.api(self.read_only).post(
            self.url("provisions/"), {"as_of": "2026-12-31"}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.code(refused), "field_write_denied")
        self.assertIn("Provision date", refused.data["message"])
        self.assertEqual(DoubtfulDebtProvision.objects.count(), before)

    def test_a_read_only_narration_refuses_only_when_one_is_sent(self):
        path = self.url("provisions/")
        refused = self.api(self.narration_read_only).post(
            path, {"as_of": "2026-12-31", "narration": "Because"}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIn("Narration", refused.data["message"])
        self.assertFalse(DoubtfulDebtProvision.objects.exists())

        saved = self.api(self.narration_read_only).post(
            path, {"as_of": "2026-12-31", "narration": ""}, format="json")
        self.assertEqual(saved.status_code, 201, saved.data)
        self.assertEqual(saved.data["data"]["narration"], "")
        self.assertIn("narration", saved.data["data"]["_read_only_fields"])

    def test_the_detail_names_the_read_only_fields_for_a_form_to_grey(self):
        run = self.run_of()
        detail = self.api(self.read_only).get(self.url(f"provisions/{run.pk}/")).data["data"]
        self.assertIn("as_of", detail["_read_only_fields"])
        self.assertIn("narration", detail["_read_only_fields"])
        self.assertIn("required_total", detail["_read_only_fields"])
        full = self.api(self.open).get(self.url(f"provisions/{run.pk}/")).data["data"]
        self.assertIn("required_total", full["_read_only_fields"])
        self.assertNotIn("as_of", full["_read_only_fields"])

    def test_an_open_caller_raises_a_run_and_is_told_its_number(self):
        self.bill(self.eze, 1_000_000, on=D(2026, 1, 5), due=D(2026, 1, 10), service=None)
        response = self.api(self.open).post(
            self.url("provisions/"), {"as_of": "2026-12-31", "narration": "Year end"},
            format="json")
        self.assertEqual(response.status_code, 201, response.data)
        number = response.data["data"]["document_number"]
        self.assertTrue(number)
        self.assertEqual(response.data["message"], f"Provision {number} prepared.")

    def test_a_hidden_status_leaves_the_responses_of_a_run_and_a_post_while_the_number_stays(self):
        from vs_finance.approvals import ensure_tenant_approval_templates

        ensure_tenant_approval_templates(self.tenant, with_default_stages=False)
        self.bill(self.eze, 1_000_000, on=D(2026, 1, 5), due=D(2026, 1, 10), service=None)
        created = self.api(self.no_status).post(
            self.url("provisions/"), {"as_of": "2026-12-31"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        run = DoubtfulDebtProvision.objects.get()
        self.assertEqual(created.data["message"], f"Provision {run.document_number} prepared.")
        self.assertEqual(created.data["data"]["document_number"], run.document_number)
        self.assertNotIn("status", created.data["data"])

        posted = self.api(self.no_status).post(
            self.url(f"provisions/{run.pk}/post/"),
            {"confirm_without_approval": True, "reason": "Year end"}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["message"], f"Provision {run.document_number} posted.")
        self.assertEqual(posted.data["data"]["document_number"], run.document_number)
        self.assertNotIn("status", posted.data["data"])
        run.refresh_from_db()
        self.assertEqual(run.status, "POSTED")

    # -- single branch, other tenant ------------------------------------------ #

    def test_a_single_branch_school_always_shows_the_line_branch(self):
        run = self.run_of(self.solo_books)
        path = self.url(f"provisions/{run.pk}/", self.solo_books)
        shown = self.api(self.solo_open).get(path).data["data"]["lines"]
        self.assertEqual([row["branch_id"] for row in shown], [self.solo_main.pk])

    def test_another_schools_caller_reads_neither_a_run_nor_its_hidden_state(self):
        run = self.run_of()
        response = self.api(self.solo_open).get(self.url(f"provisions/{run.pk}/"))
        self.assertIn(response.status_code, (403, 404))


# --------------------------------------------------------------------------- #
# Customer deposits                                                           #
# --------------------------------------------------------------------------- #

class DepositFieldAccessTests(_AccrualsFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        k = f"{DEPOSIT}."
        cls.open = cls.staff(cls.tenant, "dp-open")
        cls.no_customer = cls.staff(cls.tenant, "dp-no-customer", hide=[k + "customer"])
        cls.no_amount = cls.staff(cls.tenant, "dp-no-amount", hide=[k + "amount"])
        cls.no_status = cls.staff(cls.tenant, "dp-no-status", hide=[k + "status"])
        cls.no_outcome = cls.staff(cls.tenant, "dp-no-outcome",
                                   hide=[k + "release_note", k + "forfeiture", k + "invoice",
                                         k + "claim_opened_on"])
        cls.no_customer_invoice = cls.staff(cls.tenant, "dp-no-customer-invoice",
                                            hide=[k + "customer", k + "invoice"])
        cls.solo_open = cls.staff(cls.solo_tenant, "dp-solo-open")

    def held(self):
        self.deposit_bill(self.eze)
        self.deposit_bill(self.bello)

    # -- reads ---------------------------------------------------------------- #

    def test_each_hidden_deposit_field_leaves_the_list_with_all_its_names(self):
        self.held()
        cases = (
            (self.no_customer, ("customer_id", "customer_code", "customer_name", "customer")),
            (self.no_amount, ("amount", "amount_naira")),
            (self.no_status, ("status",)),
            (self.no_outcome, ("release_note_number", "forfeiture_id", "invoice_id",
                               "invoice_number", "claim_opened_on")),
        )
        for user, names in cases:
            rows = self.api(user).get(self.url("deposits/")).data["data"]
            self.assertEqual(len(rows), 2)
            for row in rows:
                for name in names:
                    with self.subTest(user=user.email, name=name):
                        self.assertNotIn(name, row)
        for user in (self.open, self.no_customer, self.no_amount):
            for row in self.api(user).get(self.url("deposits/")).data["data"]:
                self.assertIn("branch_id", row)
                self.assertIn("branch_name", row)
        full = self.api(self.open).get(self.url("deposits/")).data["data"][0]
        for name in ("customer_code", "branch_name", "amount", "amount_naira", "status",
                     "claim_opened_on", "release_note_number", "forfeiture_id", "invoice_number"):
            self.assertIn(name, full)

    def test_a_filter_on_a_hidden_deposit_field_is_refused(self):
        self.held()
        cases = (
            (self.no_customer, "&customer=EZE", "Customer"),
            (self.no_status, "&status=HELD", "Status"),
        )
        for user, query, label in cases:
            with self.subTest(query=query):
                response = self.api(user).get(self.url("deposits/") + query)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_read_denied")
                self.assertIn(label, response.data["message"])

    # -- the release ---------------------------------------------------------- #

    def test_an_open_release_returns_the_credit_note_with_its_customer_branch_and_amount(self):
        self.held()
        response = self.api(self.open).post(
            self.url("deposits/release/"), {"customer": "EZE"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["message"], "Released EZE's deposits.")
        (note,) = response.data["data"]["credit_notes"]
        self.assertEqual(note["customer_code"], "EZE")
        self.assertEqual(note["branch_name"], "Ikeja Branch")
        self.assertEqual(note["amount"], 50_000)
        self.assertEqual(note["amount_naira"], "₦500.00")
        self.assertTrue(note["document_number"])

    def test_a_release_drops_the_amount_a_caller_may_not_read_and_keeps_the_branch(self):
        self.held()
        no_amount = self.api(self.no_amount).post(
            self.url("deposits/release/"), {"customer": "BELLO"}, format="json")
        (note,) = no_amount.data["data"]["credit_notes"]
        self.assertNotIn("amount", note)
        self.assertNotIn("amount_naira", note)
        self.assertEqual(note["customer_code"], "BELLO")
        self.assertEqual(note["branch_name"], "Lekki Branch")

    def test_a_caller_who_cannot_read_the_customer_cannot_release_by_customer(self):
        self.held()
        notes = CreditNote.objects.count()
        response = self.api(self.no_customer).post(
            self.url("deposits/release/"), {"customer": "EZE"}, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(self.code(response), "field_read_denied")
        self.assertEqual(CreditNote.objects.count(), notes)
        self.assertFalse(CustomerDeposit.objects.exclude(status=DepositStatus.HELD).exists())

    # -- the forfeit run ------------------------------------------------------ #

    def unclaimed(self):
        """Bello's paid deposit is due to be forfeited; Eze's bill is unpaid, so his is skipped."""
        from vs_finance.customers import set_customer_active

        paid = self.deposit_bill(self.bello)
        owed = self.deposit_bill(self.eze)
        self.receipt(self.bello, 50_000, on=D(2026, 1, 12), invoice=paid)
        for customer in (self.bello, self.eze):
            set_customer_active(customer, False)
        CustomerDeposit.objects.update(claim_opened_on=D(2020, 6, 30))
        return owed

    def forfeit(self, user):
        return self.api(user).post(
            self.url("deposits/forfeit/"), {"as_of": "2026-06-30"}, format="json")

    def test_an_open_forfeit_run_names_each_forfeiture_and_each_skipped_deposit(self):
        owed = self.unclaimed()
        response = self.forfeit(self.open)
        self.assertEqual(response.status_code, 200, response.data)
        (forfeiture,) = response.data["data"]["forfeitures"]
        self.assertEqual(sorted(forfeiture), ["amount", "branch_id", "id", "journal_id"])
        self.assertEqual(forfeiture["amount"], 50_000)
        (skipped,) = response.data["data"]["skipped"]
        self.assertEqual(skipped["customer"], "EZE")
        self.assertEqual(skipped["invoice"], owed.document_number)
        self.assertEqual(skipped["reason"], "Its bill is not fully paid.")

    def test_a_forfeit_run_drops_each_value_the_caller_may_not_read(self):
        self.unclaimed()
        no_forfeiture = self.forfeit(self.no_outcome).data["data"]
        (row,) = no_forfeiture["forfeitures"]
        self.assertNotIn("id", row)
        self.assertNotIn("journal_id", row)
        self.assertIn("amount", row)
        (skipped,) = no_forfeiture["skipped"]
        self.assertNotIn("invoice", skipped)
        self.assertEqual(skipped["customer"], "EZE")

    def test_a_forfeit_run_names_no_customer_or_bill_the_caller_may_not_read(self):
        self.unclaimed()
        data = self.forfeit(self.no_customer_invoice).data["data"]
        (skipped,) = data["skipped"]
        self.assertNotIn("customer", skipped)
        self.assertNotIn("invoice", skipped)
        self.assertEqual(skipped["reason"], "Its bill is not fully paid.")
        self.assertEqual(len(data["forfeitures"]), 1)

    def test_a_forfeit_run_names_no_amount_the_caller_may_not_read(self):
        self.unclaimed()
        (row,) = self.forfeit(self.no_amount).data["data"]["forfeitures"]
        self.assertNotIn("amount", row)
        self.assertIn("branch_id", row)

    def test_the_forfeit_run_is_whole_tenant_and_a_branch_bound_caller_is_refused(self):
        self.unclaimed()
        pinned = self.staff(self.tenant, "dp-lekki", branch=self.lekki)
        self.assertEqual(self.forfeit(pinned).status_code, 403)
        self.assertTrue(CustomerDeposit.objects.filter(status=DepositStatus.HELD).exists())

    # -- single branch, other tenant ------------------------------------------ #

    def test_a_single_branch_school_always_shows_the_deposit_branch(self):
        self.deposit_bill(self.ada)
        path = self.url("deposits/", self.solo_books)
        shown = self.api(self.solo_open).get(path).data["data"]
        self.assertEqual([row["branch_id"] for row in shown], [self.solo_main.pk])

    def test_closing_the_customer_in_one_school_leaves_the_other_open_and_serves_no_foreign_row(self):
        self.deposit_bill(self.eze)
        self.deposit_bill(self.ada)
        corona = self.api(self.no_customer).get(self.url("deposits/")).data["data"]
        harbour = self.api(self.solo_open).get(self.url("deposits/", self.solo_books)).data["data"]
        self.assertTrue(all("customer_code" not in row for row in corona))
        self.assertEqual([row["customer_code"] for row in harbour], ["ADA"])
        self.assertEqual(self.api(self.solo_open).get(
            self.url("deposits/", self.books)).status_code, 404)


# --------------------------------------------------------------------------- #
# Receivables policy                                                          #
# --------------------------------------------------------------------------- #

class ReceivablesPolicyFieldAccessTests(_AccrualsFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        k = f"{SETTINGS}."
        cls.open = cls.staff(cls.tenant, "rp-open")
        cls.no_recognition = cls.staff(cls.tenant, "rp-no-recognition",
                                       hide=[k + "revenue_recognition"])
        cls.no_split_or_last = cls.staff(cls.tenant, "rp-no-split",
                                         hide=[k + "payer_payment_split", k + "last_changed"])
        cls.read_only = cls.staff(cls.tenant, "rp-read-only", read_only=[
            k + "provision_bands", k + "revenue_recognition"])
        cls.pinned = cls.staff(cls.tenant, "rp-lekki", branch=cls.lekki)
        cls.solo_open = cls.staff(cls.solo_tenant, "rp-solo-open")
        cls.solo_no_deposits = cls.staff(cls.solo_tenant, "rp-solo-no-deposits",
                                         hide=[k + "unclaimed_deposit_years"])

    path = "settings/receivables/"

    def get(self, user, books=None):
        return self.api(user).get(self.url(self.path, books)).data["data"]

    # -- reads ---------------------------------------------------------------- #

    def test_a_hidden_setting_leaves_its_label_options_and_consumer_entry(self):
        data = self.get(self.no_recognition)
        settings = data["settings"]
        for name in ("revenue_recognition", "revenue_recognition_label",
                     "revenue_recognition_options"):
            self.assertNotIn(name, settings)
        self.assertNotIn("revenue_recognition", data["consumers"])
        self.assertIn("provision_bands", settings)
        self.assertIn("provision_bands", data["consumers"])
        full = self.get(self.open)
        self.assertEqual(full["settings"]["revenue_recognition"], "SPREAD_MONTHLY")
        self.assertIn("revenue_recognition", full["consumers"])

    def test_hidden_choice_settings_and_the_last_change_leave_with_their_options(self):
        settings = self.get(self.no_split_or_last)["settings"]
        for name in ("payer_payment_split", "payer_payment_split_options", "updated_at",
                     "updated_by"):
            self.assertNotIn(name, settings)
        self.assertIn("payer_payment_surplus_options", settings)

    def test_the_settings_block_names_what_the_caller_may_read_and_not_change(self):
        settings = self.get(self.read_only)["settings"]
        self.assertIn("provision_bands", settings["_read_only_fields"])
        self.assertIn("revenue_recognition", settings["_read_only_fields"])
        self.assertIn("updated_at", settings["_read_only_fields"])
        self.assertNotIn("unclaimed_deposit_years", settings["_read_only_fields"])

    # -- the history ---------------------------------------------------------- #

    def make_history(self):
        path = self.url(self.path)
        self.assertEqual(self.api(self.open).patch(path, {
            "revenue_recognition": "AT_PERIOD_START", "unclaimed_deposit_years": 3,
        }, format="json").status_code, 200)
        self.assertEqual(self.api(self.open).patch(path, {
            "revenue_recognition": "SPREAD_MONTHLY"}, format="json").status_code, 200)

    def test_the_history_leaves_out_a_change_that_touched_only_hidden_settings(self):
        self.make_history()
        full = self.get(self.open)["history"]
        self.assertEqual(len(full), 2)

        history = self.get(self.no_recognition)["history"]
        self.assertEqual(len(history), 1)
        (entry,) = history
        self.assertEqual([change["field"] for change in entry["changes"]],
                         ["unclaimed_deposit_years"])
        self.assertNotIn("revenue_recognition", entry["before"])
        self.assertNotIn("revenue_recognition", entry["after"])
        self.assertEqual(entry["message"], "Updated 1 setting.")

    def test_a_change_nobody_hid_keeps_its_stored_message(self):
        self.make_history()
        messages = [entry["message"] for entry in self.get(self.open)["history"]]
        self.assertIn("Updated 2 finance receivables settings.", messages)

    def test_the_audit_trail_filters_the_policys_snapshots_too(self):
        self.make_history()
        rows = self.api(self.no_recognition).get(self.url("audit-logs/")).data["data"]
        policy_rows = [row for row in rows if row["target_type"] == "FinanceReceivablesPolicy"]
        self.assertTrue(policy_rows)
        for row in policy_rows:
            self.assertNotIn("revenue_recognition", row["before"])
            self.assertNotIn("revenue_recognition", row["after"])
        open_rows = self.api(self.open).get(self.url("audit-logs/")).data["data"]
        self.assertTrue(any(
            "revenue_recognition" in row["after"]
            for row in open_rows if row["target_type"] == "FinanceReceivablesPolicy"))

    # -- writes --------------------------------------------------------------- #

    def test_a_read_only_setting_refuses_a_change_before_anything_is_saved(self):
        refused = self.api(self.read_only).patch(
            self.url(self.path),
            {"provision_bands": [{"over_days": 30, "rate_bps": 100}],
             "unclaimed_deposit_years": 4}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.code(refused), "field_write_denied")
        self.assertIn("Provision bands", refused.data["message"])
        self.assertFalse(FinanceReceivablesPolicy.objects.filter(entity=self.books).exists())

    def test_a_non_writer_is_refused_before_they_are_told_the_value_is_invalid(self):
        refused = self.api(self.read_only).patch(
            self.url(self.path), {"revenue_recognition": "SOMETIMES"}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIn("Revenue recognition", refused.data["message"])

    def test_sending_a_read_only_setting_back_exactly_as_stored_is_not_a_change(self):
        stored = self.get(self.read_only)["settings"]
        saved = self.api(self.read_only).patch(self.url(self.path), {
            "provision_bands": stored["provision_bands"],
            "revenue_recognition": stored["revenue_recognition"],
            "unclaimed_deposit_years": 4,
        }, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(FinanceReceivablesPolicy.objects.get(entity=self.books)
                         .unclaimed_deposit_years, 4)

    def test_a_writable_setting_saves_and_the_response_follows_the_read_switches(self):
        saved = self.api(self.no_split_or_last).patch(
            self.url(self.path),
            {"revenue_recognition": "AT_PERIOD_START", "deposits_offset_unpaid_bills": True},
            format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        settings = saved.data["data"]["settings"]
        self.assertNotIn("payer_payment_split", settings)
        self.assertNotIn("updated_at", settings)
        self.assertEqual(settings["revenue_recognition"], "AT_PERIOD_START")
        self.assertTrue(settings["deposits_offset_unpaid_bills"])
        policy = FinanceReceivablesPolicy.objects.get(entity=self.books)
        self.assertEqual(policy.revenue_recognition, "AT_PERIOD_START")

    def test_a_hidden_setting_cannot_be_changed_by_naming_it(self):
        refused = self.api(self.no_recognition).patch(
            self.url(self.path), {"revenue_recognition": "AT_PERIOD_START"}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertFalse(FinanceReceivablesPolicy.objects.filter(entity=self.books).exists())

    def test_an_echo_of_a_setting_the_caller_cannot_read_is_refused_too(self):
        refused = self.api(self.no_recognition).patch(
            self.url(self.path), {"revenue_recognition": "SPREAD_MONTHLY"}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)

    def test_the_last_change_is_never_writable(self):
        refused = self.api(self.open).patch(
            self.url(self.path), {"updated_at": "2020-01-01T00:00:00Z"}, format="json")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIn("Last changed", refused.data["message"])

    def test_a_branch_bound_caller_still_cannot_write_and_the_write_gate_is_unchanged(self):
        refused = self.api(self.pinned).patch(
            self.url(self.path), {"unclaimed_deposit_years": 3}, format="json")
        self.assertEqual(refused.status_code, 403)
        self.assertNotEqual(self.code(refused), "field_write_denied")

    def test_the_access_map_lists_the_policy_settings(self):
        mapped = field_access_payload(self.read_only, self.tenant)[SETTINGS]
        self.assertEqual(
            sorted(set(mapped["read_only"]) & {"provision_bands", "revenue_recognition"}),
            ["provision_bands", "revenue_recognition"])
        self.assertEqual(mapped["hidden"], [])

    # -- single branch, other tenant ------------------------------------------ #

    def test_a_single_branch_school_and_a_rival_are_filtered_by_their_own_roles(self):
        solo = self.get(self.solo_no_deposits, self.solo_books)["settings"]
        self.assertNotIn("unclaimed_deposit_years", solo)
        self.assertIn("revenue_recognition", solo)
        corona = self.get(self.open)["settings"]
        self.assertIn("unclaimed_deposit_years", corona)
        self.assertEqual(self.api(self.solo_open).get(
            self.url(self.path, self.books)).status_code, 404)
