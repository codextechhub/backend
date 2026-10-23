"""The chart of accounts explains itself, marks what the software keeps, and adds accounts guided.

Corona Group runs Ikeja, Lekki and Yaba; Single Site runs one branch; Acme
Ltd is a company, not a school. A new bursar at Corona opens the chart and
sees what each account is for, which ones the software posts to by itself,
and can add "Rent" without knowing that expenses live in the 5000s.
"""
from __future__ import annotations

import importlib
import re

from django.apps import apps as django_apps
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from vs_finance.account_guide import (
    _GENERIC_SUGGESTIONS,
    ACCOUNT_PURPOSES,
    AccountSuggestion,
    ManagedBy,
    account_suggestions,
    ensure_code_not_reserved,
    managed_accounts,
    register_account_suggestion_provider,
    reserved_account_codes,
    suggest_account_code,
)
from vs_finance.constants import AccountMappingKey, AccountType
from vs_finance.exceptions import BankAccountSplitError
from vs_finance.models import (
    Account,
    BankAccount,
    FinanceAccountMapping,
    LedgerEntity,
    PettyCashFund,
)
from vs_finance.seed import DEFAULT_CHART, seed_chart_of_accounts
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, make_branch, set_field_access
from vs_tenants.models import Tenant

MIGRATION = "vs_finance.migrations.0077_account_purposes"
EM_DASH = chr(0x2014)
SCHOOL_WORDS = re.compile(
    r"\b(student|students|pupil|pupils|term|terms|tuition|class|classes|guardian|guardians"
    r"|session|sessions)\b",
    re.IGNORECASE,
)


def _asset(entity, code, name, parent_code="1000"):
    return Account.objects.create(
        entity=entity, code=code, name=name, account_type=AccountType.ASSET,
        parent=Account.objects.get(entity=entity, code=parent_code),
    )


class PurposeTextTests(SimpleTestCase):
    """The words themselves, which need no database."""

    def test_every_default_account_has_a_purpose(self):
        self.assertEqual(
            {code for code, *_ in DEFAULT_CHART} - set(ACCOUNT_PURPOSES), set(),
        )
        self.assertEqual(
            set(ACCOUNT_PURPOSES) - {code for code, *_ in DEFAULT_CHART}, set(),
            "A purpose is written for a code the default chart does not have.",
        )

    def test_the_purposes_use_neutral_words_and_no_em_dash(self):
        for code, text in ACCOUNT_PURPOSES.items():
            with self.subTest(code=code):
                self.assertEqual(SCHOOL_WORDS.findall(text), [])
                self.assertNotIn(EM_DASH, text)
                self.assertTrue(text.strip())

    def test_the_catalogue_uses_neutral_words_and_no_em_dash(self):
        for item in _GENERIC_SUGGESTIONS:
            with self.subTest(key=item.key):
                text = f"{item.name} {item.description}"
                self.assertEqual(SCHOOL_WORDS.findall(text), [])
                self.assertNotIn(EM_DASH, text)

    def test_the_migrations_frozen_text_is_the_seeds_text(self):
        migration = importlib.import_module(MIGRATION)
        self.assertEqual(migration.PURPOSES, ACCOUNT_PURPOSES)

    def test_reserved_codes_are_the_chart_and_every_role_default(self):
        reserved = reserved_account_codes()
        self.assertEqual({code for code, *_ in DEFAULT_CHART} - set(reserved), set())
        for code in ("1127", "1128", "2180"):
            self.assertIn(code, reserved, "A platform-only role's code is reserved too.")
        self.assertEqual(reserved["2195"], "Unidentified Receipts")
        self.assertEqual(reserved["1127"], "Payment provider balance")

    def test_a_catalogue_key_is_listed_once_when_a_provider_repeats_it(self):
        def repeat(entity):
            return [AccountSuggestion("rent", "Rent again", "EXPENSE", "Duplicate.")]

        register_account_suggestion_provider(repeat)
        register_account_suggestion_provider(repeat)
        try:
            rows = [item for item in account_suggestions(entity=None) if item.key == "rent"]
        finally:
            from vs_finance import account_guide

            account_guide._SUGGESTION_PROVIDERS.remove(repeat)
        self.assertEqual([item.name for item in rows], ["Rent"])


class _GuideBooks(_FinanceBranchFixture):
    """Corona's books (three branches), Single Site's (one) and Acme Ltd's (a company)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.acme_tenant = Tenant.objects.create(
            name="Acme Ltd", slug="acme-guide", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        cls.acme_branch = make_branch(cls.acme_tenant, name="Head Office")
        cls.acme_books = cls.build_books("ACMEGUIDE", cls.acme_tenant)

        # Corona keeps one bank ledger under the cash heading.
        cls.bank_ledger = _asset(cls.books, "1105", "Ikeja collections", "1100")
        cls.bank = BankAccount.objects.create(
            entity=cls.books, branch=cls.ikeja, gl_account=cls.bank_ledger,
            name="Ikeja GTBank",
        )

        keys = ("finance.account.view", "finance.account.create", "finance.account.update")
        cls.manager = cls.grant(
            cls.user_for(cls.tenant, "manager@guide.test"), *keys,
            tenant=cls.tenant, role_key="guide-manager",
        )
        cls.solo_manager = cls.grant(
            cls.user_for(cls.solo_tenant, "solo-manager@guide.test"), *keys,
            tenant=cls.solo_tenant, role_key="guide-solo-manager",
        )
        cls.acme_manager = cls.grant(
            cls.user_for(cls.acme_tenant, "manager@acme-guide.test"), *keys,
            tenant=cls.acme_tenant, role_key="guide-acme-manager",
        )
        cls.viewer = cls.grant(
            cls.user_for(cls.tenant, "viewer@guide.test"), "finance.account.view",
            tenant=cls.tenant, role_key="guide-viewer",
        )

    @staticmethod
    def code_of(rows):
        return {row["code"]: row for row in rows}


class ManagedAccountsTests(_GuideBooks):

    def test_documents_lock_receivables_and_gr_ir_against_hand_journals(self):
        managed = managed_accounts(self.books)
        for code in ("1200", "2150", "2100", "2200", "1400"):
            with self.subTest(code=code):
                account = Account.objects.get(entity=self.books, code=code)
                self.assertFalse(managed[account.pk].hand_journals)

    def test_year_end_and_payroll_accounts_are_kept_but_not_locked(self):
        managed = managed_accounts(self.books)
        for code in ("3200", "2330", "5210", "1900", "4900", "2160"):
            with self.subTest(code=code):
                account = Account.objects.get(entity=self.books, code=code)
                self.assertTrue(managed[account.pk].hand_journals)
                self.assertTrue(managed[account.pk].keeper)
                self.assertTrue(managed[account.pk].moved_by)

    def test_the_everyday_accounts_are_not_listed(self):
        managed = managed_accounts(self.books)
        everyday = ("1000", "1100", "1110", "1500", "3100", "4100", "4910", "5100",
                    "5200", "5300", "5400", "5500", "2000", "5000")
        for code in everyday:
            with self.subTest(code=code):
                self.assertNotIn(Account.objects.get(entity=self.books, code=code).pk, managed)

    def test_a_bank_and_a_petty_cash_ledger_stay_in_the_everyday_view(self):
        fund_ledger = _asset(self.books, "1115", "Ikeja petty cash", "1110")
        PettyCashFund.objects.create(
            entity=self.books, branch=self.ikeja, gl_account=fund_ledger,
            name="Ikeja float", custodian_name="Mrs Adeyemi",
        )
        managed = managed_accounts(self.books)
        self.assertNotIn(self.bank_ledger.pk, managed)
        self.assertNotIn(fund_ledger.pk, managed)

    def test_a_locked_entry_is_the_same_text_the_ledger_lock_gives(self):
        from vs_finance.control_accounts import control_accounts

        receivable = Account.objects.get(entity=self.books, code="1200")
        self.assertEqual(
            managed_accounts(self.books)[receivable.pk],
            ManagedBy(*control_accounts(self.books)[receivable.pk], False),
        )

    def test_a_mapping_override_moves_the_classification_to_the_new_account(self):
        main = Account.objects.get(entity=self.solo_books, code="3200")
        replacement = Account.objects.create(
            entity=self.solo_books, code="3250", name="Accumulated surplus",
            account_type=AccountType.EQUITY,
            parent=Account.objects.get(entity=self.solo_books, code="3000"),
        )
        self.assertIn(main.pk, managed_accounts(self.solo_books))
        FinanceAccountMapping.objects.create(
            entity=self.solo_books, key=AccountMappingKey.RETAINED_EARNINGS,
            account=replacement,
        )
        managed = managed_accounts(self.solo_books)
        self.assertNotIn(main.pk, managed)
        self.assertTrue(managed[replacement.pk].hand_journals)

    def test_the_cost_does_not_grow_with_the_chart(self):
        def queries(entity):
            with CaptureQueriesContext(connection) as ctx:
                managed_accounts(entity)
            return len(ctx)

        small = queries(self.acme_books)
        Account.objects.bulk_create([
            Account(entity=self.acme_books, code=str(5600 + n), name=f"Cost {n}",
                    account_type=AccountType.EXPENSE, normal_balance="DEBIT")
            for n in range(60)
        ])
        self.assertEqual(queries(self.acme_books), small)


class SuggestAccountCodeTests(_GuideBooks):

    def test_the_next_code_is_ten_above_the_highest_taken_in_the_types_thousand(self):
        expected = {
            AccountType.ASSET: "1910", AccountType.LIABILITY: "2410",
            AccountType.EQUITY: "3210", AccountType.INCOME: "4920",
            AccountType.EXPENSE: "5540",
        }
        for kind, code in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(suggest_account_code(self.books, kind), code)
                self.assertEqual(suggest_account_code(self.solo_books, str(kind)), code)

    def test_a_code_the_books_add_moves_the_suggestion_on(self):
        Account.objects.create(
            entity=self.books, code="5547", name="Fuel", account_type=AccountType.EXPENSE,
        )
        self.assertEqual(suggest_account_code(self.books, "EXPENSE"), "5550")

    def test_a_reserved_code_the_books_lack_still_counts_as_taken(self):
        bare = LedgerEntity.objects.create(
            name="Bare", code="BAREGUIDE", kind=LedgerEntity.Kind.TENANT, tenant=self.acme_tenant,
        )
        self.assertEqual(suggest_account_code(bare, "EXPENSE"), "5540")

    def test_a_full_end_of_range_falls_back_to_the_lowest_free_code(self):
        Account.objects.create(
            entity=self.books, code="5995", name="Late cost", account_type=AccountType.EXPENSE,
        )
        self.assertEqual(suggest_account_code(self.books, "EXPENSE"), "5001")

    def test_a_range_with_no_free_code_is_refused_in_words(self):
        held = set(Account.objects.filter(entity=self.acme_books).values_list("code", flat=True))
        Account.objects.bulk_create([
            Account(entity=self.acme_books, code=str(code), name=f"Cost {code}",
                    account_type=AccountType.EXPENSE, normal_balance="DEBIT")
            for code in range(5001, 6000) if str(code) not in held
        ])
        with self.assertRaises(ValidationError) as caught:
            suggest_account_code(self.acme_books, "EXPENSE")
        self.assertIn("taken", str(caught.exception.detail))

    def test_an_unknown_type_is_refused(self):
        with self.assertRaises(ValidationError):
            suggest_account_code(self.books, "PROFIT")


class SeededPurposeTests(_GuideBooks):

    def test_a_new_chart_carries_every_purpose(self):
        for entity in (self.books, self.solo_books):
            for code, text in ACCOUNT_PURPOSES.items():
                self.assertEqual(
                    Account.objects.get(entity=entity, code=code).description, text,
                )

    def test_a_re_seed_fills_a_blank_and_keeps_what_a_person_wrote(self):
        Account.objects.filter(entity=self.books, code="1200").update(description="")
        Account.objects.filter(entity=self.books, code="2100").update(description="Mine")
        seed_chart_of_accounts(self.books)
        self.assertEqual(
            Account.objects.get(entity=self.books, code="1200").description,
            ACCOUNT_PURPOSES["1200"],
        )
        self.assertEqual(Account.objects.get(entity=self.books, code="2100").description, "Mine")

    def test_the_migration_fills_blanks_on_every_set_of_books_and_keeps_edits(self):
        migration = importlib.import_module(MIGRATION)
        Account.objects.filter(code="1200").update(description="")
        Account.objects.filter(entity=self.rival_books, code="1200").update(
            description="Our own words")
        migration.fill_blank_purposes(django_apps, None)
        for entity in (self.books, self.solo_books, self.acme_books):
            self.assertEqual(
                Account.objects.get(entity=entity, code="1200").description,
                ACCOUNT_PURPOSES["1200"],
            )
        self.assertEqual(
            Account.objects.get(entity=self.rival_books, code="1200").description,
            "Our own words",
        )
        self.assertEqual(
            Account.objects.get(entity=self.books, code="4100").description,
            ACCOUNT_PURPOSES["4100"],
        )


class ReservedCodeTests(_GuideBooks):

    def test_a_reserved_code_the_books_lack_is_refused_with_the_next_free_one(self):
        with self.assertRaises(ValidationError) as caught:
            ensure_code_not_reserved(self.books, "1127")
        message = str(caught.exception.detail["code"])
        self.assertIn("Code 1127 is kept for the system account Payment provider balance.", message)
        self.assertIn("Use 1910 instead.", message)

    def test_a_reserved_code_the_books_hold_is_left_to_the_already_exists_check(self):
        ensure_code_not_reserved(self.books, "2195")

    def test_a_code_the_software_does_not_need_passes(self):
        ensure_code_not_reserved(self.books, "5547")

    def test_a_service_can_keep_its_own_error_type(self):
        with self.assertRaises(BankAccountSplitError):
            ensure_code_not_reserved(self.books, "1128", error=BankAccountSplitError)


class SplitReservedCodeTests(_GuideBooks):
    """A bank split names new ledger codes, so it must not take a reserved one."""

    def test_a_split_refuses_a_ledger_code_the_software_keeps(self):
        import datetime

        from vs_finance.bank_splits import split_shared_bank_account

        legacy_ledger = _asset(self.books, "1150", "Shared ledger", "1100")
        legacy = BankAccount.objects.create(
            entity=self.books, branch=None, gl_account=legacy_ledger, name="Shared GTBank",
            is_primary=True, is_primary_collection=True,
        )

        def row(branch, name, code, primary):
            return {
                "branch": branch, "opening_balance": 0,
                "bank_account_name": f"{name} bank", "ledger_account_code": code,
                "ledger_account_name": f"{name} ledger",
                "is_primary": primary, "is_primary_collection": True,
            }

        with self.assertRaisesMessage(
            BankAccountSplitError, "Code 1127 is kept for the system account",
        ):
            split_shared_bank_account(
                legacy,
                [row(self.ikeja, "Ikeja", "1127", True), row(self.lekki, "Lekki", "1152", False)],
                split_date=datetime.date(2026, 1, 15), agreement_reference="MINUTES-1",
            )


class AccountApiTests(_GuideBooks):

    def setUp(self):
        super().setUp()
        self.api = TenantAPIClient(user=self.manager)
        self.url = f"/v1/finance/accounts/?entity={self.books.code}"

    def tree(self):
        response = self.api.get(self.url + "&with_balance=true")
        self.assertEqual(response.status_code, 200, response.data)
        return self.code_of(response.data["data"])

    # -- managed_by on every read ------------------------------------------- #

    def test_the_chart_says_which_accounts_the_software_keeps(self):
        rows = self.tree()
        self.assertEqual(rows["1200"]["managed_by"]["hand_journals"], False)
        self.assertTrue(rows["1200"]["managed_by"]["keeper"])
        self.assertTrue(rows["1200"]["managed_by"]["moved_by"])
        self.assertEqual(rows["3200"]["managed_by"]["hand_journals"], True)
        self.assertEqual(rows["2330"]["managed_by"]["hand_journals"], True)
        for code in ("4100", "1100", "5300", "1000", "1105"):
            with self.subTest(code=code):
                self.assertIsNone(rows[code]["managed_by"])

    def test_the_account_picker_carries_it_too(self):
        response = self.api.get(self.url + "&page_size=100")
        self.assertEqual(response.status_code, 200, response.data)
        rows = self.code_of(response.data["data"])
        self.assertEqual(rows["2150"]["managed_by"]["hand_journals"], False)
        self.assertIsNone(rows["5300"]["managed_by"])

    def test_the_detail_and_an_edit_carry_it(self):
        receivable = Account.objects.get(entity=self.books, code="1200")
        detail = self.api.get(
            f"/v1/finance/accounts/{receivable.pk}/?entity={self.books.code}")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertFalse(detail.data["data"]["account"]["managed_by"]["hand_journals"])
        edited = self.api.patch(
            f"/v1/finance/accounts/{receivable.pk}/?entity={self.books.code}",
            {"subtype": "Current asset"}, format="json",
        )
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertIn("keeper", edited.data["data"]["managed_by"])

    def test_a_serializer_without_the_map_answers_null_and_asks_nothing(self):
        from vs_finance.serializers import AccountSerializer

        account = Account.objects.get(entity=self.books, code="1200")
        with CaptureQueriesContext(connection) as ctx:
            data = AccountSerializer(account).data
        self.assertIsNone(data["managed_by"])
        self.assertLessEqual(len(ctx), 3)

    def test_reading_the_chart_costs_the_same_however_long_it_is(self):
        for label, suffix in (("chart", "&with_balance=true"), ("picker", "&page_size=100")):
            with self.subTest(shape=label):
                self.api.get(self.url + suffix)
                with CaptureQueriesContext(connection) as before:
                    self.api.get(self.url + suffix)
                Account.objects.bulk_create([
                    Account(entity=self.books, code=str(5600 + n), name=f"{label} cost {n}",
                            account_type=AccountType.EXPENSE, normal_balance="DEBIT")
                    for n in range(40)
                ])
                with CaptureQueriesContext(connection) as after:
                    self.api.get(self.url + suffix)
                self.assertEqual(len(after), len(before))
                Account.objects.filter(
                    entity=self.books, name__startswith=f"{label} cost").delete()

    # -- guided create ------------------------------------------------------- #

    def test_an_account_can_be_added_by_type_alone(self):
        response = self.api.post(
            self.url, {"name": "Rent", "account_type": "EXPENSE"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        self.assertEqual(data["code"], "5540")
        self.assertEqual(data["account_type"], "EXPENSE")
        self.assertEqual(data["parent_code"], "5000")
        self.assertIsNone(data["managed_by"])

    def test_a_single_branch_school_adds_the_same_way(self):
        api = TenantAPIClient(user=self.solo_manager)
        response = api.post(
            f"/v1/finance/accounts/?entity={self.solo_books.code}",
            {"name": "Rent", "account_type": "expense"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["code"], "5540")

    def test_a_named_parent_beats_the_default_heading(self):
        parent = Account.objects.get(entity=self.books, code="5300")
        response = self.api.post(
            self.url, {"name": "Rent", "account_type": "EXPENSE", "parent": parent.pk},
            format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["parent_code"], "5300")

    def test_without_a_code_the_type_is_required(self):
        for body in ({"name": "Rent"}, {"name": "Rent", "account_type": "PROFIT"}):
            with self.subTest(body=body):
                response = self.api.post(self.url, body, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("account type", str(response.data).lower())

    def test_a_statutory_line_is_kept_and_checked(self):
        saved = self.api.post(
            self.url,
            {"name": "Rent", "account_type": "EXPENSE", "ifrs_line": "ADMIN_EXPENSES"},
            format="json")
        self.assertEqual(saved.status_code, 201, saved.data)
        self.assertEqual(saved.data["data"]["ifrs_line"], "ADMIN_EXPENSES")
        refused = self.api.post(
            self.url, {"name": "Fuel", "account_type": "EXPENSE", "ifrs_line": "MADE_UP"},
            format="json")
        self.assertEqual(refused.status_code, 400, refused.data)

    def test_a_code_given_keeps_the_old_rules(self):
        saved = self.api.post(self.url, {"code": "5547", "name": "Fuel"}, format="json")
        self.assertEqual(saved.status_code, 201, saved.data)
        self.assertEqual(saved.data["data"]["account_type"], "EXPENSE")
        for body in ({"code": "5547", "name": "Again"}, {"code": "55", "name": "Short"},
                     {"code": "5548", "name": " "}):
            with self.subTest(body=body):
                self.assertEqual(self.api.post(self.url, body, format="json").status_code, 400)

    def test_a_code_the_software_keeps_is_refused_with_the_next_free_one(self):
        Account.objects.filter(entity=self.books, code="2195").delete()
        response = self.api.post(self.url, {"code": "2195", "name": "Parents' deposits"},
                                 format="json")
        self.assertEqual(response.status_code, 400, response.data)
        message = str(response.data)
        self.assertIn("Code 2195 is kept for the system account Unidentified Receipts.", message)
        self.assertIn("Use 2410 instead.", message)
        self.assertFalse(Account.objects.filter(entity=self.books, code="2195").exists())

    def test_a_reserved_code_the_books_hold_reports_that_it_exists(self):
        response = self.api.post(self.url, {"code": "2195", "name": "Again"}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("already exists", str(response.data))

    # -- next code ----------------------------------------------------------- #

    def test_next_code_answers_for_the_type(self):
        response = self.api.get(
            f"/v1/finance/accounts/next-code/?entity={self.books.code}&account_type=EXPENSE")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], {"code": "5540"})

    def test_next_code_needs_a_real_type(self):
        for suffix in ("", "&account_type=PROFIT"):
            with self.subTest(suffix=suffix):
                response = self.api.get(
                    f"/v1/finance/accounts/next-code/?entity={self.books.code}{suffix}")
                self.assertEqual(response.status_code, 400, response.data)

    # -- suggestions --------------------------------------------------------- #

    def suggestions(self, api, books):
        response = api.get(f"/v1/finance/accounts/suggestions/?entity={books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["name"]: row for row in response.data["data"]}

    def test_a_school_is_offered_school_accounts_beside_the_generic_ones(self):
        self.assertEqual(self.books.tenant.kind, Tenant.Kind.SCHOOL)
        for api, books in ((self.api, self.books),
                           (TenantAPIClient(user=self.solo_manager), self.solo_books)):
            rows = self.suggestions(api, books)
            self.assertEqual(rows["Tuition fees"]["account_type"], "INCOME")
            self.assertEqual(rows["Rent"]["account_type"], "EXPENSE")
            self.assertEqual(rows["Vehicles"]["ifrs_line"], "PPE")
            self.assertEqual(
                set(rows["Rent"]), {"key", "name", "account_type", "description",
                                    "ifrs_line", "already_added"},
            )

    def test_a_company_is_offered_only_the_generic_ones(self):
        rows = self.suggestions(TenantAPIClient(user=self.acme_manager), self.acme_books)
        self.assertIn("Rent", rows)
        self.assertNotIn("Tuition fees", rows)
        self.assertNotIn("Laboratory supplies", rows)

    def test_a_suggestion_already_in_the_chart_says_so(self):
        Account.objects.create(
            entity=self.books, code="5547", name="  rent ", account_type=AccountType.EXPENSE,
        )
        rows = self.suggestions(self.api, self.books)
        self.assertTrue(rows["Rent"]["already_added"])
        self.assertFalse(rows["Fuel and generator"]["already_added"])
        self.assertFalse(self.suggestions(
            TenantAPIClient(user=self.solo_manager), self.solo_books)["Rent"]["already_added"])

    # -- who may ask --------------------------------------------------------- #

    def test_someone_who_may_only_view_cannot_create_or_ask_how(self):
        viewer = TenantAPIClient(user=self.viewer)
        code = self.books.code
        self.assertEqual(
            viewer.post(self.url, {"name": "Rent", "account_type": "EXPENSE"},
                        format="json").status_code, 403)
        self.assertEqual(viewer.get(
            f"/v1/finance/accounts/next-code/?entity={code}&account_type=EXPENSE").status_code, 403)
        self.assertEqual(viewer.get(
            f"/v1/finance/accounts/suggestions/?entity={code}").status_code, 403)
        self.assertEqual(viewer.get(self.url + "&with_balance=true").status_code, 200)

    def test_another_schools_books_are_not_reachable_by_entity(self):
        for path in ("next-code/?account_type=EXPENSE&", "suggestions/?"):
            with self.subTest(path=path):
                response = self.api.get(
                    f"/v1/finance/accounts/{path}entity={self.rival_books.code}")
                self.assertEqual(response.status_code, 404, response.data)
        post = self.api.post(
            f"/v1/finance/accounts/?entity={self.rival_books.code}",
            {"name": "Rent", "account_type": "EXPENSE"}, format="json")
        self.assertEqual(post.status_code, 404, post.data)


class ManagedByFieldAccessTests(_GuideBooks):
    """``managed_by`` follows the "Managed by the system" Read switch."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.field_keys = install_declared_fields("finance.account")
        cls.open = cls.staff("fa-open")
        cls.hidden = cls.staff("fa-hidden", hide=["finance.account.managed_by"])
        cls.no_line = cls.staff("fa-no-line", hide=["finance.account.ifrs_line"])

    @classmethod
    def staff(cls, name, *, hide=()):
        user = cls.grant(
            cls.user_for(cls.tenant, f"{name}@guide-fa.test"),
            "finance.account.view", "finance.account.create", "finance.account.update",
            tenant=cls.tenant, role_key=name,
        )
        role = TenantRoleTemplate.objects.get(tenant=cls.tenant, key=name)
        set_field_access(role, *cls.field_keys, read=True, write=True)
        if hide:
            set_field_access(role, *hide, read=False, write=False)
        return user

    def url(self, path=""):
        return f"/v1/finance/accounts/{path}?entity={self.books.code}"

    def test_the_marker_is_registered_as_a_read_only_ledger_field(self):
        from vs_rbac.models import FieldDefinition

        definition = FieldDefinition.objects.get(key="finance.account.managed_by")
        self.assertFalse(definition.writable)
        self.assertEqual(definition.label, "Managed by the system")
        self.assertEqual(definition.group, "Ledger")

    def test_a_role_that_may_read_it_sees_it(self):
        rows = TenantAPIClient(user=self.open).get(self.url()).data["data"]
        self.assertTrue(all("managed_by" in row for row in rows))

    def test_a_role_that_may_not_loses_it_from_every_read(self):
        api = TenantAPIClient(user=self.hidden)
        for shape in ("", "&with_balance=true", "&page_size=100"):
            with self.subTest(shape=shape):
                rows = api.get(self.url() + shape).data["data"]
                self.assertTrue(rows)
                self.assertTrue(all("managed_by" not in row for row in rows))
                self.assertTrue(all("code" in row for row in rows))
        receivable = Account.objects.get(entity=self.books, code="1200")
        detail = api.get(self.url(f"{receivable.pk}/")).data["data"]["account"]
        self.assertNotIn("managed_by", detail)

    def test_hidden_marker_does_not_hide_the_rest_of_a_created_account(self):
        created = TenantAPIClient(user=self.hidden).post(
            self.url(), {"name": "Rent", "account_type": "EXPENSE"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertNotIn("managed_by", created.data["data"])
        self.assertEqual(created.data["data"]["code"], "5540")

    def test_a_hidden_report_line_is_left_out_of_what_a_create_returns(self):
        created = TenantAPIClient(user=self.no_line).post(
            self.url(),
            {"name": "Rent", "account_type": "EXPENSE", "ifrs_line": "ADMIN_EXPENSES"},
            format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertNotIn("ifrs_line", created.data["data"])
        self.assertEqual(
            Account.objects.get(entity=self.books, code="5540").ifrs_line, "ADMIN_EXPENSES")
