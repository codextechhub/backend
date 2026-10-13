"""Field Access over a branch's address and email on a school's own screens.

A branch's name identifies it everywhere and has no switch. Its address and
email each have a Read switch of ``school.branches``, open by default, and no
Write switch because CodeX alone provisions and corrects a branch.

Corona has two branches, Ikeja and Lekki; Sunrise has one. Mrs Adeyemi, Corona's
bursar, may read branch addresses but not their emails; Mr Bello, a teacher, may
read neither. Corona's choices never reach Sunrise, and never reach the CodeX
staff who administer both.
"""
from datetime import date

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from core.test_utils import TenantAPIClient
from vs_audit.models import AuditEvent
from vs_audit.serializers import AuditEventDetailSerializer
from vs_finance.documents import _issuer_block
from vs_finance.models import LedgerEntity
from vs_import_data.datasets import may_import
from vs_rbac.field_enforcement import field_access_payload
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_branch,
    make_permission,
    make_platform_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
    platform_tenant,
    set_field_access,
)

RESOURCE = "school.branches"
ADDRESS = f"{RESOURCE}.address"
EMAIL = f"{RESOURCE}.email"

VIEW = "school.branches.view"


def school_reader(school, email, *, closed=()):
    """A school-wide account holding the branches view key, with *closed* switches off."""
    user = make_school_admin(None, email=email, tenant=school.tenant)
    role = make_role(school, name=f"Reader {email}")
    make_role_permission(role, make_permission(VIEW))
    make_assignment(school, user, role, branch=None)
    if closed:
        set_field_access(role, *closed, read=False, write=False)
    return user


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        install_declared_fields(RESOURCE)

        cls.corona = make_school(slug="corona-branches", name="Corona School")
        cls.ikeja = make_branch(
            cls.corona, name="Ikeja Branch", is_main=True,
            address="14 Lantern Road, Ikeja", email="ikeja@corona.test",
        )
        cls.lekki = make_branch(
            cls.corona, name="Lekki Branch", is_main=False,
            address="3 Admiralty Way, Lekki", email="lekki@corona.test",
        )
        cls.sunrise = make_school(slug="sunrise-branches", name="Sunrise School")
        cls.main = make_branch(
            cls.sunrise, name="Main Branch", is_main=True,
            address="9 Sunrise Close", email="main@sunrise.test",
        )

        cls.open_admin = school_reader(cls.corona, "open@corona.test")
        cls.bursar = school_reader(cls.corona, "adeyemi@corona.test", closed=(EMAIL,))
        cls.teacher = school_reader(cls.corona, "bello@corona.test", closed=(ADDRESS, EMAIL))
        cls.no_address = school_reader(cls.corona, "no-address@corona.test", closed=(ADDRESS,))
        cls.sunrise_open = school_reader(cls.sunrise, "open@sunrise.test")
        cls.sunrise_closed = school_reader(
            cls.sunrise, "closed@sunrise.test", closed=(ADDRESS, EMAIL),
        )

        cls.operator = make_vision_user(email="operator@codexng.test")
        role = make_platform_role(name="Branch manager")
        for permission in (
            "platform.branches.view", "platform.branches.update",
            "platform.branches.create", "platform.schools.view",
        ):
            make_platform_role_permission(role, make_permission(permission))
        make_platform_assignment(cls.operator, role)

    def api(self, user):
        return TenantAPIClient(user=user)

    def listing(self, user):
        response = self.api(user).get(reverse("my-branch-list"))
        self.assertEqual(response.status_code, 200, response.data)
        return {row["name"]: row for row in response.data["data"]}

    def detail(self, user, branch):
        response = self.api(user).get(reverse("my-branch-detail", kwargs={"code": branch.code}))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class ReadingBranchesTests(_Fixture):
    def test_an_open_role_reads_name_address_and_email_on_the_list_and_the_detail(self):
        rows = self.listing(self.open_admin)
        self.assertEqual(set(rows), {"Ikeja Branch", "Lekki Branch"})
        self.assertEqual(rows["Ikeja Branch"]["address"], "14 Lantern Road, Ikeja")
        self.assertEqual(rows["Lekki Branch"]["email"], "lekki@corona.test")

        detail = self.detail(self.open_admin, self.ikeja)
        self.assertEqual(detail["address"], "14 Lantern Road, Ikeja")
        self.assertEqual(detail["email"], "ikeja@corona.test")

    def test_a_closed_email_switch_removes_only_the_email(self):
        for row in self.listing(self.bursar).values():
            self.assertNotIn("email", row)
            self.assertIn("address", row)
        detail = self.detail(self.bursar, self.lekki)
        self.assertNotIn("email", detail)
        self.assertEqual(detail["address"], "3 Admiralty Way, Lekki")

    def test_a_closed_address_switch_removes_only_the_address(self):
        for row in self.listing(self.no_address).values():
            self.assertNotIn("address", row)
            self.assertIn("email", row)
        detail = self.detail(self.no_address, self.lekki)
        self.assertNotIn("address", detail)
        self.assertEqual(detail["email"], "lekki@corona.test")

    def test_the_name_is_always_readable_and_identifies_the_branch(self):
        for row in self.listing(self.teacher).values():
            self.assertNotIn("address", row)
            self.assertNotIn("email", row)
            self.assertIn("name", row)
            self.assertIn("code", row)
            self.assertIn("is_main", row)
        self.assertEqual(self.detail(self.teacher, self.ikeja)["name"], "Ikeja Branch")

    def test_a_single_branch_school_obeys_the_same_switches(self):
        open_rows = self.listing(self.sunrise_open)
        self.assertEqual(list(open_rows), ["Main Branch"])
        self.assertEqual(open_rows["Main Branch"]["address"], "9 Sunrise Close")

        closed_rows = self.listing(self.sunrise_closed)
        self.assertEqual(list(closed_rows), ["Main Branch"])
        self.assertNotIn("address", closed_rows["Main Branch"])
        self.assertNotIn("email", closed_rows["Main Branch"])
        self.assertNotIn("address", self.detail(self.sunrise_closed, self.main))

    def test_a_switch_in_one_school_never_reaches_another(self):
        self.assertIn("address", self.listing(self.sunrise_open)["Main Branch"])
        self.assertNotIn("address", self.listing(self.teacher)["Ikeja Branch"])
        self.assertIn("address", self.listing(self.open_admin)["Ikeja Branch"])

    def test_a_school_never_reads_another_schools_branches(self):
        self.assertEqual(set(self.listing(self.sunrise_open)), {"Main Branch"})
        response = self.api(self.sunrise_open).get(
            reverse("my-branch-detail", kwargs={"code": self.lekki.code}),
        )
        self.assertEqual(response.status_code, 404, response.data)
        # Codes are numbered per school, so Ikeja's code is Sunrise's own main branch.
        same_code = self.detail(self.sunrise_open, self.ikeja)
        self.assertEqual(same_code["name"], "Main Branch")
        self.assertEqual(same_code["address"], "9 Sunrise Close")

    def test_the_me_map_tells_a_form_what_a_role_cannot_read(self):
        hidden = field_access_payload(self.teacher, self.corona.tenant)[RESOURCE]
        self.assertEqual(hidden["hidden"], ["address", "email"])
        partly = field_access_payload(self.bursar, self.corona.tenant)[RESOURCE]
        self.assertEqual(partly["hidden"], ["email"])
        self.assertEqual(partly["read_only"], ["address"])

    def test_the_switches_offer_no_write_to_a_school(self):
        from vs_rbac.models import FieldDefinition

        for name in ("address", "email"):
            row = FieldDefinition.objects.get(key=f"{RESOURCE}.{name}")
            self.assertFalse(row.writable)
            self.assertFalse(row.sensitive)
            self.assertEqual(row.default_access, {"read": True, "write": False})


class NoSchoolWritesABranchTests(_Fixture):
    """Name, address and email are CodeX's to set, by every route."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        role = make_role(cls.corona, name="Branch editor", key="branch_editor")
        for action in ("view", "create", "update", "delete"):
            make_role_permission(role, make_permission(f"school.branches.{action}"))
        cls.editor = make_school_admin(None, email="editor@corona.test", tenant=cls.corona.tenant)
        make_assignment(cls.corona, cls.editor, role, branch=None)

    def stored(self):
        branch = type(self.ikeja).objects.get(pk=self.ikeja.pk)
        return branch.name, branch.address, branch.email

    def test_the_school_branch_routes_accept_no_write(self):
        client = self.api(self.editor)
        url = reverse("my-branch-detail", kwargs={"code": self.ikeja.code})
        body = {"name": "Renamed", "address": "1 New Road", "email": "new@corona.test"}
        for method in ("patch", "put", "post", "delete"):
            with self.subTest(method=method):
                response = getattr(client, method)(url, body, format="json")
                self.assertEqual(response.status_code, 405, response.data)
        self.assertEqual(
            self.stored(), ("Ikeja Branch", "14 Lantern Road, Ikeja", "ikeja@corona.test"),
        )

    def test_the_platform_branch_routes_refuse_a_school_account(self):
        client = self.api(self.editor)
        body = {"name": "Renamed", "address": "1 New Road", "email": "new@corona.test"}
        update = client.patch(
            reverse("branch-update", kwargs={"slug": self.corona.slug, "code": self.ikeja.code}),
            body, format="json",
        )
        create = client.post(
            reverse("branch-create", kwargs={"slug": self.corona.slug}),
            {**body, "name": "Yaba Branch"}, format="json",
        )
        self.assertEqual(update.status_code, 403, update.data)
        self.assertEqual(create.status_code, 403, create.data)
        self.assertEqual(
            self.stored(), ("Ikeja Branch", "14 Lantern Road, Ikeja", "ikeja@corona.test"),
        )
        self.assertFalse(type(self.ikeja).objects.filter(name="Yaba Branch").exists())

    def test_a_school_cannot_import_branches(self):
        self.assertFalse(may_import(self.editor, "branches"))
        self.assertTrue(may_import(self.operator, "branches"))


class CodexStaffAreUnaffectedTests(_Fixture):
    """CodeX staff are governed by the platform branch keys, whatever a school switches off."""

    def test_the_platform_branch_detail_still_carries_address_and_email(self):
        response = self.api(self.operator).get(
            reverse("branch-detail", kwargs={"slug": self.corona.slug, "code": self.ikeja.code}),
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["address"], "14 Lantern Road, Ikeja")
        self.assertEqual(response.data["data"]["email"], "ikeja@corona.test")

    def test_the_platform_school_detail_still_nests_every_branch_with_its_contact(self):
        response = self.api(self.operator).get(
            reverse("school-detail", kwargs={"slug": self.corona.slug}),
        )
        self.assertEqual(response.status_code, 200, response.data)
        rows = {row["name"]: row for row in response.data["data"]["branches"]}
        self.assertEqual(rows["Lekki Branch"]["address"], "3 Admiralty Way, Lekki")
        self.assertEqual(rows["Lekki Branch"]["email"], "lekki@corona.test")

    def test_a_correction_by_codex_still_saves(self):
        response = self.api(self.operator).patch(
            reverse("branch-update", kwargs={"slug": self.corona.slug, "code": self.ikeja.code}),
            {"address": "15 Lantern Road, Ikeja"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.ikeja.refresh_from_db()
        self.assertEqual(self.ikeja.address, "15 Lantern Road, Ikeja")


class BranchAuditTrailTests(_Fixture):
    """The trail of a correction repeats a branch's contact only to readers of it."""

    def render(self, event, user):
        request = APIRequestFactory().get("/")
        request.user = user
        request.tenant = user.tenant
        return AuditEventDetailSerializer(event, context={"request": request}).data

    def test_a_branch_correction_is_shown_to_each_reader_as_their_switches_allow(self):
        response = self.api(self.operator).patch(
            reverse("branch-update", kwargs={"slug": self.corona.slug, "code": self.lekki.code}),
            {"address": "4 Admiralty Way, Lekki", "email": "lekki-new@corona.test"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        event = AuditEvent.objects.filter(
            entity_type="Branch", entity_id=str(self.lekki.pk),
        ).latest("event_at")

        full = self.render(event, self.open_admin)
        self.assertEqual(full["before_data"]["address"], "3 Admiralty Way, Lekki")
        self.assertEqual(full["diff_data"]["email"]["after"], "lekki-new@corona.test")

        partial = self.render(event, self.bursar)
        self.assertIn("address", partial["diff_data"])
        self.assertNotIn("email", partial["diff_data"])
        self.assertNotIn("email", partial["before_data"])

        closed = self.render(event, self.teacher)
        for snapshot in (closed["before_data"], closed["diff_data"]):
            self.assertNotIn("address", snapshot)
            self.assertNotIn("email", snapshot)
        self.assertEqual(closed["before_data"]["name"], "Lekki Branch")


class LetterheadTests(_Fixture):
    """A document addressed to a payer prints the issuing branch's contact, whoever sends it."""

    def test_the_issuer_block_prints_the_branch_address_and_email(self):
        entity = LedgerEntity.objects.create(
            name="Corona Books", code="CORONAB", tenant=self.corona.tenant,
        )
        issuer = _issuer_block(entity, branch=self.lekki)
        self.assertEqual(issuer["address"], "3 Admiralty Way, Lekki")
        self.assertEqual(issuer["email"], "lekki@corona.test")

    def test_the_declared_exception_says_why_the_letterhead_is_unfiltered(self):
        from vs_rbac.field_enforcement import SYSTEM_SURFACES

        reason = SYSTEM_SURFACES["vs_finance.documents._issuer_block"]
        self.assertIn("address and email", reason)
        self.assertIn("school.branches", reason)
