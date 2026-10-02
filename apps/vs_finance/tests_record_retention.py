"""Books that survive a tax audit years later.

Lagoon View runs Ikeja and Lekki; Harbour Primary runs one branch, Main. Both
keep 2026 books. In 2032 the tax authority reviews 2026: every journal must
still exist, and the figures closed for March 2026 must be provably the ones
that were closed.

* **Retention.** A posted journal, its lines and the evidence filed behind it
  cannot be deleted for six years after the end of its fiscal year, by any
  path: an instance, a queryset, a management command. A draft can. A school
  may keep its books longer, never shorter.
* **Sealed figures.** Closing March writes its balances per branch and a
  checksum of its lines; a line edited behind the ledger's back is reported,
  with the balance that was sealed and the balance now.
* **Archive.** A closed year two years past its end can be archived by a
  whole-school reader with the key: it leaves the pickers and default lists,
  stays readable on request, and can be brought back. Both acts are audited.
"""
from __future__ import annotations

import datetime
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.db.utils import Error as DatabaseError
from django.test import TestCase

from core.retention import RetentionError, hold_on
from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

from .close import close_checklist, close_fiscal_year, close_period, reopen_period
from .constants import FinanceAuditAction, PeriodStatus
from .models import (
    Account,
    FinanceAuditLog,
    FiscalPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
    LedgerEntity,
    LedgerSeal,
)
from .posting import post_journal, resolve_period
from .retention import retained_until, retention_years
from .seals import verify_entity
from .seed import seed_chart_of_accounts, seed_currencies, seed_fiscal_year

REASON = "The 2026 accounts were filed and audited."
KEYS = (
    "finance.seal.view", "finance.fiscalyear.archive", "finance.settings.view",
    "finance.settings.update", "finance.period.view", "finance.journal.view",
)
TODAY = "vs_config.clock.tenant_today"


def _books(code, tenant):
    entity = LedgerEntity.objects.create(
        name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
    )
    seed_chart_of_accounts(entity)
    seed_fiscal_year(entity, year=2026, start_month=1)
    return entity


class _Books(TestCase):
    """Lagoon View (Ikeja, Lekki) and Harbour Primary (Main), each with 2026 books."""

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.lagoon = make_school(slug="lagoon-retention", name="Lagoon View")
        cls.ikeja = make_branch(cls.lagoon, name="Ikeja Branch")
        cls.lekki = make_branch(cls.lagoon, name="Lekki Branch", is_main=False)
        cls.books = _books("LAGRET", cls.lagoon.tenant)

        role = make_role(cls.lagoon, name="Bursar")
        for key in KEYS:
            make_role_permission(role, make_permission(key))
        cls.adaeze = make_school_admin(cls.ikeja, email="adaeze@lagoon-retention.example.com")
        make_assignment(cls.lagoon, cls.adaeze, role, branch=None)
        cls.ngozi = make_school_admin(cls.lekki, email="ngozi@lagoon-retention.example.com")
        make_assignment(cls.lagoon, cls.ngozi, role, branch=cls.lekki)
        cls.tunde = make_school_admin(cls.ikeja, email="tunde@lagoon-retention.example.com")
        make_assignment(cls.lagoon, cls.tunde, make_role(cls.lagoon, name="Clerk"), branch=None)

        cls.harbour = make_school(slug="harbour-retention", name="Harbour Primary")
        cls.harbour_main = make_branch(cls.harbour, name="Main Branch")
        cls.harbour_books = _books("HBRRET", cls.harbour.tenant)
        harbour_role = make_role(cls.harbour, name="Bursar")
        for key in KEYS:
            make_role_permission(harbour_role, make_permission(key))
        cls.bola = make_school_admin(cls.harbour_main, email="bola@harbour-retention.example.com")
        make_assignment(cls.harbour, cls.bola, harbour_role, branch=None)

    def month(self, number, books=None):
        return FiscalPeriod.objects.get(
            entity=books or self.books, fiscal_year__year=2026, period_no=number,
        )

    def year(self, books=None):
        return FiscalYear.objects.get(entity=books or self.books, year=2026)

    def post(self, branch, date, pairs, books=None, *, actor=None, draft=False):
        books = books or self.books
        entry = JournalEntry.objects.create(
            entity=books, branch=branch, date=date, period=resolve_period(books, date),
            narration="test", created_by=actor,
        )
        for i, (code, debit, credit) in enumerate(pairs, start=1):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=books, code=code),
                debit=debit, credit=credit, line_no=i,
            )
        if not draft:
            post_journal(entry, actor_user=actor)
        return entry

    def fees(self, branch, day, kobo, books=None):
        return self.post(branch, day, [("1100", kobo, 0), ("4100", 0, kobo)], books)

    def client_for(self, user):
        return TenantAPIClient(user=user)


class RetentionPeriodTests(_Books):
    """Six years from the end of the record's fiscal year, or the school's longer choice."""

    def test_the_statutory_floor_is_six_years_from_the_end_of_the_fiscal_year(self):
        self.assertEqual(retention_years(self.lagoon.tenant), 6)
        self.assertEqual(
            retained_until(self.books, datetime.date(2026, 3, 10)), datetime.date(2032, 12, 31),
        )

    def test_a_school_may_keep_its_books_longer_but_never_shorter(self):
        from vs_config.exceptions import ConfigurationError
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        definition = ConfigurationDefinition.objects.get(key="finance.retention.years")
        set_value(definition=definition, value=10, actor=self.adaeze, tenant=self.lagoon.tenant)
        self.assertEqual(retention_years(self.lagoon.tenant), 10)
        self.assertEqual(retention_years(self.harbour.tenant), 6)

        with self.assertRaises(ConfigurationError):
            set_value(definition=definition, value=3, actor=self.adaeze, tenant=self.lagoon.tenant)
        self.assertEqual(retention_years(self.lagoon.tenant), 10)

    def test_a_raised_floor_wins_over_a_shorter_school_value(self):
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        own = ConfigurationDefinition.objects.get(key="finance.retention.years")
        set_value(definition=own, value=7, actor=self.adaeze, tenant=self.lagoon.tenant)
        floor = ConfigurationDefinition.objects.get(key="finance.retention.statutory_years")
        set_value(definition=floor, value=8, actor=self.adaeze)
        self.assertEqual(retention_years(self.lagoon.tenant), 8)


class NothingKeptCanBeDeletedTests(_Books):
    """Every ORM path refuses a kept row; a draft is still somebody's unfinished work."""

    def test_a_posted_journal_refuses_every_deletion_path(self):
        entry = self.fees(self.ikeja, datetime.date(2026, 3, 10), 50_000_00)

        with self.assertRaises(RetentionError) as caught, transaction.atomic():
            entry.delete()
        self.assertIn("2032-12-31", caught.exception.message)
        with self.assertRaises(RetentionError), transaction.atomic():
            JournalEntry.objects.filter(pk=entry.pk).delete()
        with self.assertRaises(RetentionError), transaction.atomic():
            JournalLine.objects.filter(entry=entry).delete()
        self.assertEqual(JournalLine.objects.filter(entry=entry).count(), 2)

    def test_a_draft_can_still_be_deleted(self):
        draft = self.post(self.ikeja, datetime.date(2026, 3, 10),
                          [("1100", 100, 0), ("4100", 0, 100)], draft=True)
        draft.delete()
        self.assertFalse(JournalEntry.objects.filter(pk=draft.pk).exists())

    def test_the_hold_ends_when_the_period_does(self):
        entry = self.fees(self.ikeja, datetime.date(2026, 3, 10), 50_000_00)
        with mock.patch(TODAY, return_value=datetime.date(2032, 12, 31)):
            self.assertIsNotNone(hold_on(entry))
        with mock.patch(TODAY, return_value=datetime.date(2033, 1, 1)):
            self.assertIsNone(hold_on(entry))

    def test_evidence_filed_against_a_kept_record_keeps_its_bytes(self):
        from django.contrib.contenttypes.models import ContentType

        from core.media import revoke
        from core.models import StoredFile
        from core.storage import DatabaseStorage

        entry = self.fees(self.ikeja, datetime.date(2026, 3, 10), 50_000_00)
        stored = StoredFile.objects.create(
            name="evidence/receipt-0001.pdf", content=b"%PDF-1.4 receipt", size=16,
            tenant=self.lagoon.tenant,
            owner_content_type=ContentType.objects.get_for_model(JournalEntry),
            owner_object_id=str(entry.pk), owner_field="file",
        )

        with self.assertRaises(RetentionError), transaction.atomic():
            DatabaseStorage().delete(stored.name)
        revoke([stored.name])
        stored.refresh_from_db()
        self.assertIsNotNone(stored.revoked_at)
        self.assertEqual(bytes(stored.content), b"%PDF-1.4 receipt")

    def test_delete_user_refuses_a_person_who_posted_to_the_books(self):
        self.post(self.ikeja, datetime.date(2026, 3, 10),
                  [("1100", 100, 0), ("4100", 0, 100)], actor=self.adaeze)

        with self.assertRaises(CommandError) as caught:
            call_command(
                "delete_user", email=[self.adaeze.email], tenant_id=self.lagoon.tenant.slug,
                force=True, stdout=StringIO(),
            )
        self.assertIn("Deactivate the account instead", str(caught.exception))
        self.assertTrue(type(self.adaeze).objects.filter(pk=self.adaeze.pk).exists())

    def test_delete_entity_refuses_books_that_must_be_kept(self):
        self.fees(self.harbour_main, datetime.date(2026, 3, 10), 100, self.harbour_books)

        with self.assertRaises(CommandError):
            call_command("delete_entity", entity=[self.harbour_books.code], force=True,
                         stdout=StringIO())
        self.assertTrue(LedgerEntity.objects.filter(pk=self.harbour_books.pk).exists())
        self.assertEqual(JournalEntry.objects.filter(entity=self.harbour_books).count(), 1)


class SealedFiguresTests(_Books):
    """Closing a month seals its figures; a later change behind the ledger is reported."""

    def close(self, number, books=None):
        close_period(books or self.books, self.month(number, books), run_depreciation=False)

    def test_closing_a_month_seals_each_branchs_balances(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        self.fees(self.lekki, datetime.date(2026, 1, 20), 70_000)
        self.close(1)

        seal = LedgerSeal.objects.get(period=self.month(1))
        self.assertEqual(seal.kind, LedgerSeal.Kind.PERIOD_CLOSED)
        self.assertEqual(seal.line_count, 4)
        cash = str(Account.objects.get(entity=self.books, code="1100").pk)
        self.assertEqual(seal.balances[str(self.ikeja.pk)][cash], [100_000, 0])
        self.assertEqual(seal.balances[str(self.lekki.pk)][cash], [70_000, 0])
        self.assertTrue(verify_entity(self.books).ok)
        closed = FinanceAuditLog.objects.get(
            entity=self.books, action=FinanceAuditAction.PERIOD_CLOSED,
        )
        self.assertEqual(closed.metadata["seal_checksum"], seal.seal_checksum)

    def test_a_line_changed_behind_the_ledger_is_reported(self):
        entry = self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        self.close(1)
        line = entry.lines.get(account__code="1100")
        JournalLine.objects.filter(pk=line.pk).update(debit=90_000)
        JournalLine.objects.filter(entry=entry, account__code="4100").update(credit=90_000)

        result = verify_entity(self.books)
        self.assertFalse(result.ok)
        check = result.mismatches[0]
        self.assertFalse(check.lines_match)
        cash = Account.objects.get(entity=self.books, code="1100").pk
        moved = next(d for d in check.differences if d["account_id"] == cash)
        self.assertEqual(moved["sealed"]["debit"], 100_000)
        self.assertEqual(moved["now"]["debit"], 90_000)
        self.assertEqual(moved["branch_id"], self.ikeja.pk)

        warning = next(
            i for i in close_checklist(self.books, self.month(2)).items
            if i.name == "sealed_figures_unchanged"
        )
        self.assertFalse(warning.passed)
        self.assertFalse(warning.blocking)

        with self.assertRaises(CommandError):
            call_command("verify_ledger_seals", entity=[self.books.code], stdout=StringIO())

    def test_seals_cannot_be_altered_or_removed(self):
        self.close(1)
        seals = LedgerSeal.objects.filter(entity=self.books)
        with self.assertRaises(DatabaseError), transaction.atomic():
            seals.update(lines_checksum="0" * 64)
        with self.assertRaises(DatabaseError), transaction.atomic():
            seals.delete()
        self.assertEqual(seals.count(), 1)

    def test_seals_chain_and_a_reopened_month_is_resealed_when_it_closes_again(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        self.close(1)
        first = LedgerSeal.objects.get(period=self.month(1))
        reopen_period(self.books, self.month(1), reason="A receipt was missed.")
        self.fees(self.ikeja, datetime.date(2026, 1, 25), 5_000)
        self.assertTrue(verify_entity(self.books).ok)  # An open month is not checked.

        self.close(1)
        second = LedgerSeal.objects.filter(period=self.month(1)).latest("pk")
        self.assertEqual(second.previous_id, first.pk)
        self.assertEqual(second.line_count, 4)
        self.assertTrue(verify_entity(self.books).ok)

    def test_a_year_close_leaves_every_sealed_month_standing(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        self.post(self.lekki, datetime.date(2026, 3, 20), [("5200", 40_000, 0), ("1100", 0, 40_000)])
        for number in range(1, 13):
            self.close(number)
        close_fiscal_year(self.books, self.year())

        self.assertTrue(LedgerSeal.objects.filter(
            fiscal_year=self.year(), period__isnull=True, kind=LedgerSeal.Kind.YEAR_CLOSED,
        ).exists())
        result = verify_entity(self.books)
        self.assertTrue(result.ok, [c.differences for c in result.mismatches])
        self.assertEqual(len(result.checks), 13)

    def test_a_one_branch_school_files_unbranched_lines_under_its_only_branch(self):
        self.fees(None, datetime.date(2026, 1, 15), 100_000, self.harbour_books)
        self.close(1, self.harbour_books)
        seal = LedgerSeal.objects.get(period=self.month(1, self.harbour_books))
        self.assertEqual(list(seal.balances), [str(self.harbour_main.pk)])

    def test_verify_endpoint_answers_a_whole_school_reader_only(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        self.close(1)
        url = f"/v1/finance/seals/verify/?entity={self.books.code}"

        response = self.client_for(self.adaeze).get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["ok"])
        self.assertEqual(response.data["data"]["checked"], 1)

        self.assertEqual(self.client_for(self.ngozi).get(url).status_code, 403)
        self.assertEqual(self.client_for(self.tunde).get(url).status_code, 403)
        foreign = self.client_for(self.bola).get(url)
        self.assertIn(foreign.status_code, (403, 404))

    def test_a_one_branch_bursar_reaches_the_whole_school(self):
        self.fees(self.harbour_main, datetime.date(2026, 1, 15), 100_000, self.harbour_books)
        self.close(1, self.harbour_books)
        response = self.client_for(self.bola).get(
            f"/v1/finance/seals/verify/?entity={self.harbour_books.code}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["ok"])


class ArchivedYearTests(_Books):
    """A closed year two years past its end is put away, never deleted."""

    LATER = datetime.date(2029, 2, 1)

    def close_year(self, books=None):
        FiscalPeriod.objects.filter(
            fiscal_year=self.year(books), is_closing=False,
        ).update(status=PeriodStatus.CLOSED)
        close_fiscal_year(books or self.books, self.year(books))

    def archive(self, user, *, books=None, reason=REASON):
        books = books or self.books
        return self.client_for(user).post(
            f"/v1/finance/fiscal-years/{self.year(books).pk}/archive/?entity={books.code}",
            {"reason": reason}, format="json",
        )

    def test_an_open_year_or_a_recent_one_cannot_be_archived(self):
        with mock.patch(TODAY, return_value=self.LATER):
            refused = self.archive(self.adaeze)
        self.assertEqual(refused.status_code, 409, refused.data)
        self.close_year()
        with mock.patch(TODAY, return_value=datetime.date(2028, 12, 30)):
            too_soon = self.archive(self.adaeze)
        self.assertEqual(too_soon.status_code, 409, too_soon.data)
        self.assertIsNone(self.year().archived_at)

    def test_archiving_hides_the_year_and_its_documents_until_asked(self):
        paid = self.fees(self.ikeja, datetime.date(2026, 3, 10), 100_000)
        self.close_year()
        with mock.patch(TODAY, return_value=self.LATER):
            response = self.archive(self.adaeze)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["is_archived"])
        self.assertTrue(FinanceAuditLog.objects.filter(
            entity=self.books, action=FinanceAuditAction.FISCAL_YEAR_ARCHIVED,
            actor=self.adaeze, metadata__reason=REASON,
        ).exists())

        client = self.client_for(self.adaeze)
        years = client.get(f"/v1/finance/fiscal-years/?entity={self.books.code}")
        self.assertEqual(years.data["data"], [])
        years = client.get(f"/v1/finance/fiscal-years/?entity={self.books.code}&include_archived=true")
        self.assertEqual([y["year"] for y in years.data["data"]], [2026])
        periods = client.get(f"/v1/finance/periods/?entity={self.books.code}")
        self.assertEqual(periods.data["data"], [])

        journals = client.get(f"/v1/finance/journals/?entity={self.books.code}")
        self.assertNotIn(paid.pk, [j["id"] for j in journals.data["data"]])
        journals = client.get(f"/v1/finance/journals/?entity={self.books.code}&include_archived=true")
        self.assertIn(paid.pk, [j["id"] for j in journals.data["data"]])

    def test_an_archived_year_must_be_unarchived_before_it_is_reopened(self):
        from .close import reopen_fiscal_year
        from .exceptions import PeriodCloseError

        self.close_year()
        with mock.patch(TODAY, return_value=self.LATER):
            self.archive(self.adaeze)
        with self.assertRaises(PeriodCloseError):
            reopen_fiscal_year(self.books, self.year(), reason="Correction.")

        back = self.client_for(self.adaeze).post(
            f"/v1/finance/fiscal-years/{self.year().pk}/unarchive/?entity={self.books.code}",
            {"reason": "The auditor needs it at hand."}, format="json",
        )
        self.assertEqual(back.status_code, 200, back.data)
        self.assertIsNone(self.year().archived_at)
        self.assertTrue(FinanceAuditLog.objects.filter(
            entity=self.books, action=FinanceAuditAction.FISCAL_YEAR_UNARCHIVED,
        ).exists())

    def test_only_a_whole_school_holder_of_the_key_archives(self):
        self.close_year()
        with mock.patch(TODAY, return_value=self.LATER):
            self.assertEqual(self.archive(self.ngozi).status_code, 403)
            self.assertEqual(self.archive(self.tunde).status_code, 403)
            self.assertEqual(self.archive(self.adaeze, reason="").status_code, 400)
        self.assertIsNone(self.year().archived_at)

    def test_a_one_branch_bursar_archives_her_own_books_only(self):
        self.close_year(self.harbour_books)
        with mock.patch(TODAY, return_value=self.LATER):
            self.assertEqual(self.archive(self.bola, books=self.harbour_books).status_code, 200)
            foreign = self.client_for(self.bola).post(
                f"/v1/finance/fiscal-years/{self.year().pk}/archive/?entity={self.books.code}",
                {"reason": REASON}, format="json",
            )
        self.assertIn(foreign.status_code, (403, 404))
        self.assertIsNone(self.year().archived_at)


class RetentionSettingsApiTests(_Books):
    """The school's own period is set by a whole-school holder of the settings key."""

    URL = "/v1/finance/settings/records/?entity={}"

    def test_reading_and_lengthening_the_period(self):
        client = self.client_for(self.adaeze)
        url = self.URL.format(self.books.code)
        read = client.get(url)
        self.assertEqual(read.status_code, 200, read.data)
        self.assertEqual(read.data["data"]["statutory_years"], 6)
        self.assertIsNone(read.data["data"]["retention_years"])
        self.assertEqual(read.data["data"]["archive_min_age_years"], 2)

        saved = client.patch(url, {"retention_years": 10, "archive_min_age_years": 3}, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(saved.data["data"]["effective_retention_years"], 10)
        self.assertEqual(saved.data["data"]["archive_min_age_years"], 3)
        self.assertTrue(FinanceAuditLog.objects.filter(
            entity=self.books, action=FinanceAuditAction.RETENTION_SETTINGS_UPDATED,
        ).exists())

        shorter = client.patch(url, {"retention_years": 4}, format="json")
        self.assertEqual(shorter.status_code, 422, shorter.data)
        cleared = client.patch(url, {"retention_years": None}, format="json")
        self.assertEqual(cleared.data["data"]["effective_retention_years"], 6)

    def test_a_branch_bursar_reads_but_cannot_change_it(self):
        url = self.URL.format(self.books.code)
        client = self.client_for(self.ngozi)
        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(client.patch(url, {"retention_years": 10}, format="json").status_code, 403)
        self.assertEqual(
            self.client_for(self.tunde).get(url).status_code, 403,
        )
        self.assertEqual(retention_years(self.lagoon.tenant), 6)
