"""A school gets its set of books at the moment it is created.

Books used to arrive only through the platform-only finance endpoint, so a
school existed until somebody remembered to provision them. Two product
decisions shape what is pinned here:

* **every** school gets books, entitled to finance or not, because adding them
  later means going back to repair every school created before;
* provisioning is **best effort**, because a school, its tenant, its first
  administrator, its branches and its entitlements are worth far more than its
  books, and the books can be created afterwards.

The second decision is the one with a trap in it, and
``test_a_books_failure_does_not_cost_us_the_school`` is the reason this file
exists. School creation runs inside ``transaction.atomic()``. An exception
raised inside that block poisons the whole transaction, so catching it is not
enough: the outer commit fails anyway and the school, the tenant, the admin user
and the branches all vanish. Only a nested atomic block (a savepoint) around the
books call keeps the failure local.
"""
from __future__ import annotations

import itertools
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_finance.models import Account, FiscalPeriod, LedgerEntity
from vs_rbac.tests.helpers import make_vision_user
from vs_tenants.models import Branch, Tenant

from .models import School
from .services.books import derive_entity_code


def _failing_statement(*args, **kwargs):
    """Fail the way a real provisioning bug fails: at the database.

    Raising a bare Python exception is not the same thing. The transaction only
    becomes unusable once a statement has actually failed, and that is the state
    a savepoint exists to escape.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM a_table_that_does_not_exist")


class _SchoolCreationMixin:
    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="school-books@example.com", super_admin=True,
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    _branch_email_counter = itertools.count(1)

    def _create(self, payload, *, expect=201):
        # Every school is created with at least one branch, so a payload that
        # does not care about branches still has to carry its main one. These
        # tests are about books and entitlements, not about branch shape.
        payload = dict(payload)
        payload.setdefault("branches", [self._branch(
            "Main Campus",
            is_main=True,
            email=f"main-{next(self._branch_email_counter)}@branch.test",
        )])
        response = self._client().post(
            reverse("school-create"), payload, format="json",
        )
        self.assertEqual(response.status_code, expect, response.data)
        return response

    def _books_for(self, school):
        return LedgerEntity.objects.filter(tenant=school.tenant).first()

    @staticmethod
    def _branch(name, *, is_main, email):
        return {
            "name": name,
            "_type": "Main" if is_main else "Annex",
            "state": "Lagos",
            "is_main": is_main,
            "primary_admin_data": {"full_name": f"{name} Head", "email": email},
        }


class SchoolBooksProvisioningTests(_SchoolCreationMixin, TestCase):
    """What creating a school now leaves behind in the ledger."""

    def test_creating_a_school_provisions_its_books(self):
        # No package setup at all, so no finance entitlement: the books arrive
        # anyway, which is the product decision this pins.
        self._create({"name": "Corona Secondary", "slug": "corona-secondary"})
        school = School.objects.get(slug="corona-secondary")

        entity = self._books_for(school)
        self.assertIsNotNone(entity)
        self.assertEqual(entity.name, "Corona Secondary")
        self.assertEqual(entity.code, "CORONASECONDARY")
        self.assertEqual(entity.kind, LedgerEntity.Kind.TENANT)
        self.assertEqual(entity.base_currency_id, "NGN")
        self.assertTrue(entity.is_active)
        # number_code is left to the model, which keeps it globally unique.
        self.assertTrue(entity.number_code)

        # And they are usable, not just present.
        self.assertTrue(Account.objects.filter(entity=entity, code="1100").exists())
        self.assertEqual(FiscalPeriod.objects.filter(entity=entity).count(), 12)

    def test_the_books_keep_the_schools_currency(self):
        self._create({
            "name": "Dollar Academy", "slug": "dollar-academy", "currency": "USD",
        })
        school = School.objects.get(slug="dollar-academy")

        self.assertEqual(self._books_for(school).base_currency_id, "USD")

    def test_a_long_slug_is_truncated_to_the_entity_code_column(self):
        slug = "saint-catherines-international-college-of-technology"
        self._create({"name": "Saint Catherine's", "slug": slug})
        school = School.objects.get(slug=slug)

        code = self._books_for(school).code
        self.assertEqual(len(code), 16)
        self.assertEqual(code, "SAINTCATHERINESI")  # the stem, cut to the column
        self.assertTrue(code.isupper())

    def test_a_colliding_entity_code_is_resolved_rather_than_raised(self):
        """Two schools with similar names is ordinary, not exceptional."""
        first = self._create({"name": "Kings College", "slug": "kings-college"})
        first_school = School.objects.get(slug="kings-college")
        self.assertEqual(self._books_for(first_school).code, "KINGSCOLLEGE")

        # A different school whose slug strips down to the same stem.
        self._create({"name": "Kings College Lagos", "slug": "kingscollege"})
        second_school = School.objects.get(slug="kingscollege")

        second_code = self._books_for(second_school).code
        self.assertEqual(second_code, "KINGSCOLLEGE2")
        self.assertEqual(LedgerEntity.objects.filter(code="KINGSCOLLEGE").count(), 1)

    def test_the_derived_code_falls_back_to_the_school_id_when_numbering_runs_out(self):
        """The terminal candidate is built from a unique key, so the derivation
        cannot run out of answers and raise on the school-creation path."""
        school = School.objects.create(name="Busy Name", slug="busy-name")
        stem = "BUSYNAME"
        taken = [stem] + [stem + str(n) for n in range(2, 100)]
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        # bulk_create skips save(), which is what we want here: the auto-derived
        # 3-character number_code would collide long before the entity codes do,
        # and it is the entity code this test is about.
        LedgerEntity.objects.bulk_create([
            LedgerEntity(
                name=f"Squatter {index}", code=code,
                kind=LedgerEntity.Kind.OTHER,
                tenant=school.tenant, base_currency_id="NGN",
                number_code=f"Z{alphabet[index // 36]}{alphabet[index % 36]}",
            )
            for index, code in enumerate(taken)
        ])

        self.assertEqual(derive_entity_code(school), f"SCH{school.pk}")

    def test_a_school_is_never_given_two_sets_of_books(self):
        """Creation provisions; anything that asks again finds the same books."""
        self._create({"name": "Once Only", "slug": "once-only"})
        school = School.objects.get(slug="once-only")
        entity = self._books_for(school)

        from .services.books import provision_books_for_school

        again = provision_books_for_school(school)

        self.assertEqual(again.pk, entity.pk)
        self.assertEqual(LedgerEntity.objects.filter(tenant=school.tenant).count(), 1)


class SchoolSurvivesBooksFailureTests(_SchoolCreationMixin, TestCase):
    """The savepoint test. If this passes with the nested atomic removed, it is
    not testing anything."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Without the prebuilt role templates, school creation refuses to
        # provision the first administrator at all (it will not create an
        # account with no role), and the assertion below would pass vacuously
        # against a user that was never created.
        call_command("seed_actions", stdout=StringIO())
        call_command("seed_prebuilt_role_templates", stdout=StringIO())

    def test_a_books_failure_does_not_cost_us_the_school(self):
        payload = {
            "name": "Resilient Academy",
            "slug": "resilient-academy",
            "primary_admin_data": {
                "full_name": "Ada Lovelace",
                "email": "ada@resilient.test",
                "phone": "+2348000000001",
            },
            "branches": [
                self._branch("Main Campus", is_main=True, email="main@resilient.test"),
                self._branch("Annex", is_main=False, email="annex@resilient.test"),
            ],
        }

        # The failure has to be a *database* failure to be worth testing. A
        # plain Python exception raised and caught leaves the transaction
        # perfectly healthy, so a try/except with no savepoint would pass such a
        # test while still being broken. A failed statement genuinely aborts the
        # transaction, and from that point every later statement fails too,
        # until something rolls back to a savepoint.
        #
        # It is also forced at the outermost point of the books work, which is
        # the case only our own savepoint covers: vs_finance wraps the seeding
        # in an atomic block of its own, so a failure *inside* it is already
        # contained, while the code derivation, the guard lookup and anything
        # this bridge does around them are not.
        with patch(
            "vs_finance.provisioning.provision_books", side_effect=_failing_statement,
        ) as provisioning_call:
            response = self._create(payload)

        self.assertTrue(provisioning_call.called)
        self.assertTrue(response.data["success"])

        # Everything the school creation transaction owns is still there.
        school = School.objects.get(slug="resilient-academy")
        self.assertEqual(school.status, "PENDING")
        tenant = Tenant.objects.get(slug="resilient-academy")
        self.assertEqual(school.tenant_id, tenant.id)

        from vs_user.models import User

        admin = User.objects.filter(email="ada@resilient.test").first()
        self.assertIsNotNone(admin, "the first administrator must survive")
        self.assertEqual(admin.tenant_id, tenant.id)

        branch_names = set(
            Branch.objects.filter(tenant=tenant).values_list("name", flat=True)
        )
        self.assertEqual(branch_names, {"Main Campus", "Annex"})

        # Only the books are missing, and they are recoverable.
        self.assertFalse(LedgerEntity.objects.filter(tenant=tenant).exists())

    def test_a_failure_partway_through_provisioning_leaves_no_half_built_books(self):
        """The other shape of failure: the entity row is already written when
        the seeding blows up. Finance rolls its own work back, and the school
        still survives."""
        with patch(
            "vs_finance.seed.seed_chart_of_accounts", side_effect=_failing_statement,
        ):
            self._create({"name": "Half Built", "slug": "half-built"})

        school = School.objects.get(slug="half-built")
        self.assertFalse(LedgerEntity.objects.filter(tenant=school.tenant).exists())
        self.assertFalse(LedgerEntity.objects.filter(code="HALFBUILT").exists())

    def test_the_failure_is_logged_with_the_tenant_so_it_can_be_found(self):
        with patch(
            "vs_finance.provisioning.provision_books",
            side_effect=RuntimeError("the ledger is on fire"),
        ):
            with self.assertLogs("schools.vs_schools.services.books", level="ERROR") as logs:
                self._create({"name": "Noisy Failure", "slug": "noisy-failure"})

        self.assertTrue(any("noisy-failure" in line for line in logs.output))
        self.assertTrue(any("the ledger is on fire" in line for line in logs.output))

    def test_a_recovered_school_can_be_backfilled(self):
        with patch(
            "vs_finance.provisioning.provision_books",
            side_effect=RuntimeError("the ledger is on fire"),
        ):
            self._create({"name": "Repair Me", "slug": "repair-me"})

        call_command("provision_school_books", "--school", "repair-me", stdout=StringIO())

        school = School.objects.get(slug="repair-me")
        self.assertTrue(LedgerEntity.objects.filter(tenant=school.tenant).exists())


class ProvisionSchoolBooksCommandTests(TestCase):
    """The operator's way to give books to schools that predate this change.

    There is deliberately no endpoint: ``finance.entity.create`` is not a key a
    School Admin holds.
    """

    def setUp(self):
        # Created straight through the ORM, so nothing provisioned books: this
        # is the shape of every school that predates the creation hook.
        self.old = School.objects.create(name="Old School", slug="old-school")
        self.other = School.objects.create(name="Other School", slug="other-school")

    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command("provision_school_books", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing(self):
        out, _ = self._run("--dry-run")

        self.assertIn("would provision", out)
        self.assertIn("OLDSCHOOL", out)
        self.assertFalse(
            LedgerEntity.objects
            .filter(tenant__in=[self.old.tenant, self.other.tenant])
            .exists()
        )

    def test_it_provisions_every_school_without_books(self):
        self._run()

        for school in (self.old, self.other):
            entity = LedgerEntity.objects.get(tenant=school.tenant)
            self.assertEqual(entity.kind, LedgerEntity.Kind.TENANT)
            self.assertEqual(FiscalPeriod.objects.filter(entity=entity).count(), 12)

    def test_it_is_idempotent(self):
        self._run()
        codes = sorted(LedgerEntity.objects.values_list("code", flat=True))

        out, _ = self._run()

        self.assertIn("already keeps books", out)
        self.assertEqual(sorted(LedgerEntity.objects.values_list("code", flat=True)), codes)

    def test_it_can_be_limited_to_one_school(self):
        self._run("--school", "old-school")

        self.assertTrue(LedgerEntity.objects.filter(tenant=self.old.tenant).exists())
        self.assertFalse(LedgerEntity.objects.filter(tenant=self.other.tenant).exists())


class BranchCountIsCountedNotStoredTests(_SchoolCreationMixin, TestCase):
    """How many sites a school runs is read off its branches, never stored.

    ``School.operates_branches`` used to record it as a flag, set at creation
    and correctable afterwards, which meant the school could say one thing while
    its branch rows said another. The flag is gone; these tests are what stops a
    replacement being added.
    """

    def test_the_school_carries_no_branch_count_flag(self):
        self._create({"name": "Just Main", "slug": "just-main"})

        school = School.objects.get(slug="just-main")
        self.assertEqual(school.branches.count(), 1)
        self.assertFalse(hasattr(school, "operates_branches"))

    def test_a_second_branch_is_visible_by_counting_it(self):
        self._create({
            "name": "Multi Site", "slug": "multi-site",
            "branches": [
                self._branch("Main Campus", is_main=True, email="main@multi-site.test"),
                self._branch("Lekki Annex", is_main=False, email="lekki@multi-site.test"),
            ],
        })

        school = School.objects.get(slug="multi-site")
        self.assertEqual(school.branches.count(), 2)
        self.assertEqual(school.branches.filter(is_main=True).count(), 1)

    def test_the_flag_is_not_accepted_on_create_or_update(self):
        """A client still sending it writes nothing and is not misled.

        DRF drops an unknown key rather than refusing it, so the check that
        matters is that no such column comes back on the read side either.
        """
        self._create({
            "name": "Old Client", "slug": "old-client",
            "operates_branches": True,
        })

        response = self._client().patch(
            reverse("school-update", args=["old-client"]),
            {"operates_branches": True, "motto": "Onwards"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("operates_branches", response.data["data"])
        detail = self._client().get(reverse("school-detail", args=["old-client"]))
        self.assertNotIn("operates_branches", detail.data["data"])

    def test_the_update_endpoint_still_refuses_to_write_status(self):
        """Activation belongs to onboarding; this endpoint never sets status."""
        self._create({"name": "Still Pending", "slug": "still-pending"})

        self._client().patch(
            reverse("school-update", args=["still-pending"]),
            {"motto": "Still going", "status": "ACTIVE"}, format="json",
        )

        self.assertEqual(School.objects.get(slug="still-pending").status, "PENDING")
