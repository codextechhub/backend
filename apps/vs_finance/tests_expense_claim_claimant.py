"""An expense claim's claimant: a member of staff from the staff list, or a typed name.

Ngozi, Corona Group's finance admin, records Tolu Fashola's claim for lab
chemicals by picking Tolu from the staff list: the claim is tied to Tolu's
account and reads "Tolu Fashola". Mrs Okafor, a PTA volunteer, is on no staff
list, so Ngozi types her name. Rival Group is another school; naming one of its
people on a Corona claim is refused exactly as an unknown id is. Single Site
runs one branch, so the same rules are shown in a school where the branch
dimension recedes.

The claimant is one Field Access switch, ``finance.expenseclaim.claimant``,
covering the link, the name, the ``claimant`` body key, the claims search, the
export columns, the approval card and the spending dashboard's oldest claims.
Each access state (hidden, read-only, writable) is one role built once in
``setUpTestData`` and driven through the real endpoints.
"""
from __future__ import annotations

import datetime
import itertools

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from core.migration_testing import RewoundSchemaTestCase
from core.test_utils import TenantAPIClient
from vs_rbac.field_enforcement import field_access_payload
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access
from vs_workflow.presentation import summary_for_reader

from .models import Account, ExpenseClaim, ExpenseClaimLine
from .tests_branch_scope import _FinanceBranchFixture

CLAIM = "finance.expenseclaim"
CLAIMANT = f"{CLAIM}.claimant"
KEYS = ("finance.expenseclaim.create", "finance.expenseclaim.view")
JAN_10 = datetime.date(2026, 1, 10)
_seq = itertools.count(1)


def _line(account="5300"):
    return {"description": "Lab chemicals", "expense_account": account,
            "quantity": 1, "unit_price": 12_500_00}


class _ClaimantFixture(_FinanceBranchFixture):
    """Corona, Single Site and Rival Group, with Tolu on Corona's staff and one role per access state."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields(CLAIM)
        users = get_user_model().objects
        cls.tolu = users.create_user(
            email="tolu.fashola@corona.test", password="pw", tenant=cls.tenant,
            status="ACTIVE", first_name="Tolu", last_name="Fashola",
        )
        cls.rival_person = users.create_user(
            email="bisi.adewale@rival.test", password="pw", tenant=cls.rival_tenant,
            status="ACTIVE", first_name="Bisi", last_name="Adewale",
        )
        cls.solo_person = users.create_user(
            email="kemi.ojo@solo.test", password="pw", tenant=cls.solo_tenant,
            status="ACTIVE", first_name="Kemi", last_name="Ojo",
        )
        cls.ngozi = cls.staff(cls.tenant)
        cls.hidden = cls.staff(cls.tenant, read=False, write=False)
        cls.read_only = cls.staff(cls.tenant, read=True, write=False)
        cls.solo_admin = cls.staff(cls.solo_tenant)

    @classmethod
    def staff(cls, tenant, *, read=True, write=True):
        """A finance user who may raise and read claims, with the claimant switch as given."""
        name = f"claimant-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@claims.test"), *KEYS,
            tenant=tenant, role_key=name,
        )
        role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
        set_field_access(role, CLAIMANT, read=read, write=write)
        return user

    @classmethod
    def typed_claim(cls, books, name, *, claimant=None, title="Bank visit"):
        """A draft claim written straight to the books, for the read-side tests."""
        claim = ExpenseClaim.objects.create(
            entity=books, branch=cls.ikeja if books == cls.books else cls.solo_main,
            claimant=claimant, claimant_name=name, claim_date=JAN_10, title=title,
        )
        ExpenseClaimLine.objects.create(
            claim=claim, line_no=1, description="Taxi", unit_price=5_000_00,
            net_amount=5_000_00, expense_account=Account.objects.get(entity=books, code="5300"),
        )
        return claim

    def url(self, path, books=None):
        return f"/v1/finance/{path}?entity={(books or self.books).code}"

    def raise_claim(self, user, books=None, **body):
        books = books or self.books
        payload = {"claim_date": "2026-01-10", "title": "Lab chemicals", "lines": [_line()], **body}
        if books == self.books:
            payload.setdefault("branch", self.ikeja.pk)
        return TenantAPIClient(user=user).post(self.url("expense-claims/", books), payload,
                                               format="json")


class ClaimantIsPickedOrTypedTests(_ClaimantFixture):

    def test_a_member_of_staff_picked_from_the_list_is_linked_and_named(self):
        response = self.raise_claim(self.ngozi, claimant=self.tolu.pk,
                                    claimant_name="what was typed before")

        self.assertEqual(response.status_code, 201, response.data)
        row = response.data["data"]
        self.assertEqual((row["claimant_id"], row["claimant_name"]), (self.tolu.pk, "Tolu Fashola"))
        claim = ExpenseClaim.objects.get(pk=row["id"])
        self.assertEqual((claim.claimant_id, claim.claimant_name), (self.tolu.pk, "Tolu Fashola"))
        self.assertEqual(
            set(ExpenseClaim.objects.filter(claimant=self.tolu).values_list("pk", flat=True)),
            {claim.pk},
        )

    def test_somebody_not_on_the_staff_list_is_a_typed_name(self):
        response = self.raise_claim(self.ngozi, claimant_name="  Mrs Okafor ")

        self.assertEqual(response.status_code, 201, response.data)
        row = response.data["data"]
        self.assertEqual((row["claimant_id"], row["claimant_name"]), (None, "Mrs Okafor"))

    def test_a_claim_with_no_claimant_is_refused_and_nothing_is_written(self):
        for body in ({}, {"claimant_name": "   "}, {"claimant": "", "claimant_name": ""}):
            with self.subTest(body=body):
                response = self.raise_claim(self.ngozi, **body)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("claimant_name", response.data["error"]["detail"])
        self.assertFalse(ExpenseClaim.objects.filter(entity=self.books).exists())

    def test_a_person_from_another_school_is_refused_like_an_unknown_one(self):
        answers = {}
        for label, ref in (("rival", self.rival_person.pk), ("unknown", 987_654),
                           ("not a number", "tolu")):
            response = self.raise_claim(self.ngozi, claimant=ref, claimant_name="Bisi Adewale")
            self.assertEqual(response.status_code, 400, response.data)
            answers[label] = response.data["error"]["detail"]
        self.assertEqual(answers["rival"], answers["unknown"])
        self.assertEqual(answers["rival"], answers["not a number"])
        self.assertIn("claimant", answers["rival"])
        self.assertFalse(ExpenseClaim.objects.filter(entity=self.books).exists())

    def test_a_school_with_one_branch_links_its_own_staff_and_refuses_another_schools(self):
        mine = self.raise_claim(self.solo_admin, self.solo_books, claimant=self.solo_person.pk)
        theirs = self.raise_claim(self.solo_admin, self.solo_books, claimant=self.tolu.pk)

        self.assertEqual(mine.status_code, 201, mine.data)
        self.assertEqual(mine.data["data"]["claimant_name"], "Kemi Ojo")
        self.assertEqual(theirs.status_code, 400, theirs.data)

    def test_a_name_longer_than_the_record_holds_is_refused(self):
        response = self.raise_claim(self.ngozi, claimant_name="A" * 161)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("claimant_name", response.data["error"]["detail"])

    def test_the_approval_card_names_the_claimant_as_before(self):
        from vs_workflow.handlers import get_handler

        claim = self.typed_claim(self.books, "Tolu Fashola", claimant=self.tolu)

        summary = get_handler("finance.expense_claim").summary(claim)

        claimant = [f for f in summary["fields"] if f["label"] == "Claimant"]
        self.assertEqual([f["value"] for f in claimant], ["Tolu Fashola"])


class ClaimantFieldAccessTests(_ClaimantFixture):
    """Hidden, read-only and writable, on every surface that carries the claimant."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tolus = cls.typed_claim(cls.books, "Tolu Fashola", claimant=cls.tolu,
                                    title="Lab chemicals")
        cls.okafors = cls.typed_claim(cls.books, "Mrs Okafor", title="Sports day drinks")

    def get(self, user, path, **params):
        query = "".join(f"&{k}={v}" for k, v in params.items())
        response = TenantAPIClient(user=user).get(self.url(path) + query)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_hidden_the_claimant_is_absent_from_the_list_and_the_detail(self):
        rows = self.get(self.hidden, "expense-claims/")
        detail = self.get(self.hidden, f"expense-claims/{self.tolus.pk}/")

        self.assertEqual({row["id"] for row in rows}, {self.tolus.pk, self.okafors.pk})
        for payload in [*rows, detail]:
            self.assertNotIn("claimant_id", payload)
            self.assertNotIn("claimant_name", payload)
            self.assertNotIn("Tolu", str(payload))
        self.assertEqual(detail["title"], "Lab chemicals")

    def test_hidden_a_search_does_not_match_on_the_claimant(self):
        self.assertEqual(self.get(self.hidden, "expense-claims/", q="Tolu"), [])
        self.assertEqual({r["id"] for r in self.get(self.hidden, "expense-claims/", q="Lab")},
                         {self.tolus.pk})
        self.assertEqual({r["id"] for r in self.get(self.ngozi, "expense-claims/", q="Tolu")},
                         {self.tolus.pk})

    def test_hidden_or_read_only_a_claim_cannot_be_raised_with_a_claimant(self):
        for user in (self.hidden, self.read_only):
            for body in ({"claimant": self.tolu.pk}, {"claimant_name": "Mrs Okafor"}):
                with self.subTest(user=user.email, body=body):
                    response = self.raise_claim(user, **body)
                    self.assertEqual(response.status_code, 403, response.data)
                    self.assertEqual(response.data["error"]["code"], "field_write_denied")
                    self.assertEqual(set(response.data["error"]["detail"]), set(body))
        self.assertEqual(ExpenseClaim.objects.filter(entity=self.books).count(), 2)

    def test_hidden_a_crafted_id_from_another_school_is_refused_for_the_switch_first(self):
        response = self.raise_claim(self.hidden, claimant=self.rival_person.pk)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")

    def test_read_only_the_claimant_shows_and_the_detail_greys_it(self):
        rows = self.get(self.read_only, "expense-claims/")
        detail = self.get(self.read_only, f"expense-claims/{self.tolus.pk}/")

        self.assertEqual({(r["claimant_id"], r["claimant_name"]) for r in rows},
                         {(self.tolu.pk, "Tolu Fashola"), (None, "Mrs Okafor")})
        self.assertEqual(set(detail["_read_only_fields"]),
                         {"claimant", "claimant_id", "claimant_name"})

    def test_writable_nothing_is_greyed(self):
        detail = self.get(self.ngozi, f"expense-claims/{self.tolus.pk}/")

        self.assertEqual(detail["_read_only_fields"], [])
        self.assertEqual(detail["claimant_name"], "Tolu Fashola")

    def test_the_form_map_lists_the_claimant_and_the_body_key_it_follows(self):
        hidden = field_access_payload(self.hidden, self.tenant)[CLAIM]
        read_only = field_access_payload(self.read_only, self.tenant)[CLAIM]

        self.assertEqual(hidden["hidden"], ["claimant_id", "claimant_name"])
        self.assertEqual(hidden["read_only"], ["claimant"])
        self.assertEqual(read_only["hidden"], [])
        self.assertEqual(read_only["read_only"], ["claimant", "claimant_id", "claimant_name"])
        self.assertNotIn(CLAIM, field_access_payload(self.ngozi, self.tenant))

    def _request(self, user):
        request = APIRequestFactory().get("/")
        request.user = user
        request.tenant = self.tenant
        return request

    def test_the_approval_card_drops_the_claimant_for_an_approver_who_may_not_read_it(self):
        from vs_workflow.handlers import get_handler

        summary = get_handler("finance.expense_claim").summary(self.tolus)

        hidden = summary_for_reader(summary, self.tenant, request=self._request(self.hidden))
        shown = summary_for_reader(summary, self.tenant, request=self._request(self.read_only))

        self.assertNotIn("Claimant", [f["label"] for f in hidden["fields"]])
        self.assertNotIn("Tolu", str(hidden))
        self.assertIn("Tolu Fashola", [f["value"] for f in shown["fields"]])

    def test_the_spending_dashboard_names_the_oldest_claims_claimant_only_to_a_reader(self):
        from .dashboard_serializers import SpendDashboardSerializer

        payload = {"claims": {"submitted": {"count": 1, "amount": "N12,500.00"}, "oldest": [
            {"id": self.tolus.pk, "claimant": "Tolu Fashola", "title": "Lab chemicals",
             "days": 12, "amount": "N12,500.00"},
        ]}}

        def render(user):
            return SpendDashboardSerializer(
                payload, context={"request": self._request(user)}).data["claims"]

        hidden, shown = render(self.hidden), render(self.read_only)

        self.assertEqual(hidden["oldest"][0]["title"], "Lab chemicals")
        self.assertNotIn("claimant", hidden["oldest"][0])
        self.assertEqual(shown["oldest"][0]["claimant"], "Tolu Fashola")
        self.assertEqual(payload["claims"]["oldest"][0]["claimant"], "Tolu Fashola")


class ClaimantExportTests(SimpleTestCase):
    """The claims export carries the claimant, and the claimant's email, under the claimant switch."""

    def test_the_claimant_columns_and_the_search_follow_the_switch(self):
        from vs_exports.catalogue import get_dataset

        dataset = get_dataset("finance.expense_claims")
        search = next(f for f in dataset.filters if f.id == "search")

        self.assertEqual(dataset.column_access("claimant_name"), CLAIMANT)
        self.assertEqual(dataset.column_access("claimant__email"), CLAIMANT)
        self.assertEqual(dataset.column_access(search.paths[1]), CLAIMANT)


class ClaimantMigrationTests(RewoundSchemaTestCase):
    """0076 changes no claim: a typed claim stays typed, and is never linked by its name.

    A claim typed "Tolu Fashola" before the staff list was offered keeps no link
    even though exactly one account at the school bears that name, both after the
    migration and after it is reversed.
    """

    APP = "vs_finance"
    BEFORE = "0075_delivery_recipient_contacts"
    AFTER = "0076_expense_claim_claimant_from_staff_list"

    def _claims(self, target):
        model = self.historical_apps(target).get_model("vs_finance", "ExpenseClaim")
        return sorted(model.objects.values_list("claimant_name", "claimant_id"))

    def test_a_typed_claim_is_unchanged_up_and_down(self):
        old = self.historical
        tenant = old.get_model("vs_tenants", "Tenant").objects.create(
            name="Migration Claims", slug="migration-claims", kind="ORGANIZATION", status="ACTIVE",
        )
        # The rewind leaves the accounts table at its latest shape, so the live model fits it.
        get_user_model().objects.create_user(
            email="tolu@migration-claims.test", password="pw", tenant_id=tenant.pk,
            first_name="Tolu", last_name="Fashola", status="ACTIVE",
        )
        entity = old.get_model("vs_finance", "LedgerEntity").objects.create(
            name="Migration Claim Books", code="MIGCLM", number_code="976", kind="TENANT",
            tenant=tenant,
        )
        Claim = old.get_model("vs_finance", "ExpenseClaim")
        for number, name in (("EXP-1", "Tolu Fashola"), ("EXP-2", "Mrs Okafor")):
            Claim.objects.create(entity=entity, document_number=number, claimant_name=name,
                                 claim_date=JAN_10)
        before = [("Mrs Okafor", None), ("Tolu Fashola", None)]
        self.assertEqual(self._claims(self.BEFORE), before)

        self.migrate_to(self.AFTER)
        self.assertEqual(self._claims(self.AFTER), before)

        self.migrate_to(self.BEFORE)
        self.assertEqual(self._claims(self.BEFORE), before)
