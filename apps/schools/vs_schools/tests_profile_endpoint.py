"""``/v1/i/me/profile/`` - the school's own view of itself.

This endpoint exists because of a gap that made an onboarding step impossible
to clear. "Complete your school profile" is required before a school can go
live, and the only endpoint that could write those fields was
``/schools/<slug>/update/``: gated on ``platform.schools.update``, which no
school admin holds, and closed to a PENDING tenant by the pending-tenant
surface. So the step could be blocked and never cleared by the person it was
addressed to. The first test below is that gap, stated as a passing assertion.
"""
import base64

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_tenants.models import BranchStatus, Tenant
from vs_user.tokens import CodeXRefreshToken

from .models import School, SchoolStatus
from .serializers import SchoolProfileSerializer


class _SchoolProfileFixture(TestCase):
    """A school, its administrators and the calls that reach its own profile."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(
            slug="bright-star", name="Bright Star", status=SchoolStatus.PENDING,
        )
        cls.branch = make_branch(
            cls.school, name="Main Branch", status=BranchStatus.PENDING,
        )
        cls.tenant = cls.school.tenant

        cls.view_perm = make_permission("school.profile.view")
        cls.update_perm = make_permission("school.profile.update")

        # The school's own administrator: posted school-wide (branch=None) and
        # holding both keys.
        cls.admin_role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(cls.admin_role, cls.view_perm)
        make_role_permission(cls.admin_role, cls.update_perm)
        cls.admin = make_school_admin(
            None, email="admin@bright-star.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.admin_role, branch=None)

        # A branch admin: may read the profile, may not change it.
        cls.branch_role = make_role(cls.school, name="Branch Admin", key="branch_admin")
        make_role_permission(cls.branch_role, cls.view_perm)
        cls.branch_admin = make_school_admin(
            cls.branch, email="branch@bright-star.example.com",
        )
        make_assignment(
            cls.school, cls.branch_admin, cls.branch_role, branch=cls.branch,
        )

    def _client(self, user):
        """A real bearer token, not ``force_authenticate``.

        This endpoint reads ``request.tenant``, and that is bound by
        ``TenantJWTAuthentication`` - which ``force_authenticate`` skips. A test
        that took the shortcut would 404 on every call for a reason the endpoint
        never has in production.
        """
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    @property
    def url(self):
        return reverse("school-profile")

    def _get(self, user, tenant_slug=None):
        return self._client(user).get(
            self.url, {"tenant": tenant_slug or self.tenant.slug},
        )

    def _patch(self, user, payload, tenant_slug=None):
        return self._client(user).patch(
            f"{self.url}?tenant={tenant_slug or self.tenant.slug}",
            payload, format="json",
        )


class SchoolProfileEndpointTests(_SchoolProfileFixture):
    """A school reading and editing its own profile, live or not."""

    # ── The gap this endpoint closes ─────────────────────────────────────────

    def test_a_pending_school_can_read_and_write_its_own_profile(self):
        """The whole point. The school is PENDING, which is when it needs this.

        Every other school endpoint answers 403 TENANT_NOT_LIVE here, which is
        what made "Complete your school profile" a step the school could be
        blocked on and never clear.
        """
        self.assertEqual(self.tenant.status, Tenant.Status.PENDING)

        read = self._get(self.admin)
        self.assertEqual(read.status_code, 200, read.data)

        write = self._patch(self.admin, {"motto": "Knowledge and Light"})
        self.assertEqual(write.status_code, 200, write.data)
        self.school.refresh_from_db()
        self.assertEqual(self.school.motto, "Knowledge and Light")

    # ── What the school may not change about itself ──────────────────────────

    def test_name_slug_and_code_are_shown_but_never_accepted(self):
        """CodeX allocates the identity; the school reads it.

        The slug in particular is the host every one of this school's users
        signs in at, so moving it stays a platform decision. A payload that
        names it is not an error, but it must not move anything.
        """
        before = (self.school.name, self.school.slug, self.school.code)

        response = self._patch(self.admin, {
            "name": "Hijacked School",
            "slug": "hijacked",
            "code": "XX-1",
            # One real change, so the write is not refused as a no-op.
            "website": "https://bright-star.example.com",
        })
        self.assertEqual(response.status_code, 200, response.data)

        self.school.refresh_from_db()
        self.assertEqual(
            (self.school.name, self.school.slug, self.school.code), before,
        )
        self.assertEqual(self.school.website, "https://bright-star.example.com")

    def test_the_payload_says_which_fields_are_editable(self):
        response = self._get(self.admin)
        editable = response.data["data"]["editable_fields"]
        self.assertIn("ownership_type", editable)
        self.assertIn("currency", editable)
        for locked in ("name", "slug", "code", "status"):
            self.assertNotIn(locked, editable)

    # ── Who may do what ──────────────────────────────────────────────────────

    def test_a_branch_admin_may_read_the_profile(self):
        """Currency and term structure govern screens a branch admin uses."""
        response = self._get(self.branch_admin)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_branch_admin_may_not_change_the_school(self):
        """The school's identity is not a branch-level decision."""
        response = self._patch(self.branch_admin, {"motto": "Branch admin was here"})
        self.assertEqual(response.status_code, 403, response.data)
        self.school.refresh_from_db()
        self.assertNotEqual(self.school.motto, "Branch admin was here")

    def test_another_tenant_is_not_reachable_from_here(self):
        """There is no identifier to change, and asserting a foreign tenant 404s.

        404 rather than 403, so a caller cannot enumerate which slugs exist.
        """
        other = make_school(slug="green-field", name="Green Field")
        make_branch(other, name="Main Branch")

        response = self._get(self.admin, tenant_slug=other.slug)
        self.assertEqual(response.status_code, 404, response.data)

        other.refresh_from_db()
        self.assertEqual(other.name, "Green Field")

    # ── What the screen needs to render ──────────────────────────────────────

    def test_missing_required_names_the_unfilled_fields(self):
        """Read from the model, which is the same list the go-live gate uses."""
        School.objects.filter(pk=self.school.pk).update(ownership_type="")

        response = self._get(self.admin)
        missing = response.data["data"]["missing_required"]
        self.assertEqual(
            [row["field"] for row in missing],
            ["email", "phone", "address", "ownership_type"],
        )
        self.assertEqual(missing[-1]["label"], "Ownership type")

    def test_missing_required_is_the_same_answer_the_onboarding_gate_gives(self):
        """One list, two readers. Two copies would eventually disagree."""
        from schools.vs_onboarding.services.conditions import condition_holds

        School.objects.filter(pk=self.school.pk).update(currency="")
        self.school.refresh_from_db()

        response = self._get(self.admin)
        self.assertTrue(response.data["data"]["missing_required"])
        self.assertFalse(
            condition_holds("SCHOOL_METADATA", self.tenant, self.school),
        )

    def test_options_ship_with_the_record(self):
        """So a form cannot offer a value the model will refuse."""
        response = self._get(self.admin)
        options = response.data["data"]["options"]
        self.assertEqual(
            {row["value"] for row in options["currency"]}, {"NGN", "USD"},
        )
        self.assertIn(
            "3_TERMS", {row["value"] for row in options["term_structure"]},
        )

    def test_an_unknown_choice_is_refused(self):
        response = self._patch(self.admin, {"currency": "GBP"})
        self.assertEqual(response.status_code, 400, response.data)
        self.school.refresh_from_db()
        self.assertEqual(self.school.currency, "NGN")


class LiveSchoolProfileEndpointTests(TestCase):
    """The same endpoint once the school has gone live.

    The profile stays the school's to maintain, except for the two fields every
    fee, invoice and academic session is laid out in. Those are fixed from
    go-live, and only CodeX, through the platform's own update endpoint, moves
    them.

    Not a subclass of the pending tests: it needs a different school, and
    inheriting would run every pending test a second time against it.
    """

    @classmethod
    def setUpTestData(cls):
        # Stamped the way go-live stamps it, so "has been live" survives a suspension.
        cls.school = make_school(
            slug="corona-live", name="Corona Live", status=SchoolStatus.ACTIVE,
            activated_at=timezone.now(),
        )
        cls.branch = make_branch(cls.school, name="Main Branch")
        cls.tenant = cls.school.tenant

        view_perm = make_permission("school.profile.view")
        update_perm = make_permission("school.profile.update")
        role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(role, view_perm)
        make_role_permission(role, update_perm)
        cls.admin = make_school_admin(
            None, email="admin@corona-live.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, role, branch=None)

    def _client(self, user):
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def _get(self):
        return self._client(self.admin).get(
            reverse("school-profile"), {"tenant": self.tenant.slug},
        )

    def _patch(self, payload):
        return self._client(self.admin).patch(
            f"{reverse('school-profile')}?tenant={self.tenant.slug}",
            payload, format="json",
        )

    def test_a_live_school_reads_its_profile(self):
        self.assertTrue(self.school.has_ever_been_live())
        response = self._get()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], SchoolStatus.ACTIVE)

    def test_editable_fields_drop_currency_and_term_structure(self):
        editable = self._get().data["data"]["editable_fields"]
        self.assertNotIn("currency", editable)
        self.assertNotIn("term_structure", editable)
        for field in ("ownership_type", "address", "website", "motto", "logo"):
            self.assertIn(field, editable)

    def test_the_address_is_still_the_schools_to_change(self):
        response = self._patch({"address": "12 Allen Avenue, Ikeja"})
        self.assertEqual(response.status_code, 200, response.data)
        self.school.refresh_from_db()
        self.assertEqual(self.school.address, "12 Allen Avenue, Ikeja")

    def test_currency_and_term_structure_are_refused_by_field(self):
        response = self._patch({
            "currency": "USD", "term_structure": "2_SEMESTERS", "motto": "Onward",
        })
        self.assertEqual(response.status_code, 400, response.data)
        detail = response.data["error"]["detail"]
        message = "This is fixed once the school is live. Contact XVS to change it."
        self.assertEqual(detail["currency"], [message])
        self.assertEqual(detail["term_structure"], [message])

        self.school.refresh_from_db()
        self.assertEqual(self.school.currency, "NGN")
        self.assertEqual(self.school.term_structure, "3_TERMS")
        self.assertNotEqual(self.school.motto, "Onward")

    def test_sending_the_stored_value_back_is_not_a_change(self):
        """A form that posts every field it shows must not trip on the locked two."""
        response = self._patch({
            "currency": self.school.currency,
            "term_structure": self.school.term_structure,
            "website": "https://corona-live.example.com",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.school.refresh_from_db()
        self.assertEqual(self.school.website, "https://corona-live.example.com")

    def test_a_suspended_school_that_was_live_stays_locked(self):
        """Live at any point, the same test that freezes the slug."""
        School.objects.filter(pk=self.school.pk).update(status=SchoolStatus.SUSPENDED)
        self.assertTrue(School.objects.get(pk=self.school.pk).has_ever_been_live())
        self.assertNotIn(
            "currency",
            SchoolProfileSerializer(School.objects.get(pk=self.school.pk)).data["editable_fields"],
        )

    def test_codex_can_still_change_them_through_the_platform_endpoint(self):
        operator = make_vision_user(email="operator@codex.example.com", super_admin=True)
        client = APIClient()
        client.force_authenticate(user=operator)
        response = client.patch(
            reverse("school-update", kwargs={"slug": self.school.slug}),
            {"currency": "USD", "term_structure": "2_SEMESTERS"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.school.refresh_from_db()
        self.assertEqual(self.school.currency, "USD")
        self.assertEqual(self.school.term_structure, "2_SEMESTERS")


#: The smallest valid PNG there is. Real bytes, because the upload validator
#: checks the leading signature - a file of zeroes named ``.png`` is refused, and
#: rightly.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class SchoolLogoEndpointTests(_SchoolProfileFixture):
    """``/v1/i/me/profile/logo/`` - setting and clearing the school's own logo.

    Shares the profile fixture rather than rebuilding it: the logo is part of
    the same profile, gated on the same key, and reached by the same school.
    """

    @property
    def logo_url(self):
        return reverse("school-profile-logo")

    def _upload(self, user, content=ONE_PIXEL_PNG, name="logo.png"):
        return self._client(user).post(
            f"{self.logo_url}?tenant={self.tenant.slug}",
            {"logo": SimpleUploadedFile(name, content, content_type="image/png")},
            format="multipart",
        )

    def _delete(self, user):
        return self._client(user).delete(
            f"{self.logo_url}?tenant={self.tenant.slug}",
        )

    def test_a_pending_school_can_set_its_own_logo(self):
        """Same reason as the profile itself: this is when a school brands up."""
        self.assertEqual(self.tenant.status, Tenant.Status.PENDING)

        response = self._upload(self.admin)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["logo"])

        self.school.refresh_from_db()
        self.assertTrue(self.school.branding.logo)

    def test_the_whole_profile_comes_back_not_just_the_logo(self):
        """The caller's next screen lists what is still missing."""
        response = self._upload(self.admin)
        body = response.data["data"]
        self.assertIn("missing_required", body)
        self.assertIn("options", body)
        self.assertEqual(body["name"], self.school.name)

    def test_a_file_that_is_not_an_image_is_refused(self):
        """The extension is a claim; the leading bytes are the evidence.

        A script renamed ``logo.png`` must not reach storage - it is served back
        to every user of this school in the app shell.
        """
        response = self._upload(
            self.admin, content=b"<script>alert(1)</script>", name="logo.png",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.school.refresh_from_db()
        self.assertFalse(getattr(self.school, "branding", None) and self.school.branding.logo)

    def test_a_mislabelled_image_says_why_in_the_message(self):
        """The screen shows ``message``, so the reason has to be in it.

        A JPEG saved from the web as ``crest.png`` passes the browser's type
        check on its name alone. The refusal has to tell the admin the file is
        not the PNG it claims, not "An error occurred".
        """
        response = self._upload(
            self.admin, content=b"\xff\xd8\xff\xe0" + b"\x00" * 12, name="crest.png",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            response.data["message"],
            "This file is not really a PNG. Open it and save it again as PNG, "
            "then upload the new copy.",
        )

    def test_a_jfif_is_told_to_rename_rather_than_refused_as_the_wrong_type(self):
        """A ``.jfif`` is a JPEG under another name, so the fix is the rename."""
        response = self._upload(
            self.admin, content=b"\xff\xd8\xff\xe0" + b"\x00" * 12, name="crest.jfif",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            response.data["message"],
            "This is a JPG saved with a .jfif name. Rename it to end in .jpg, "
            "then upload it.",
        )

    def test_a_pdf_is_refused_even_though_documents_allow_one(self):
        """A logo is rendered inline. There is no such thing as a PDF logo."""
        response = self._upload(self.admin, content=b"%PDF-1.4 x", name="logo.pdf")
        self.assertEqual(response.status_code, 400, response.data)

    def test_an_oversized_logo_is_refused_with_its_own_limit(self):
        big = ONE_PIXEL_PNG + b"\x00" * (2 * 1024 * 1024 + 1)
        response = self._upload(self.admin, content=big)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("2MB", str(response.data))

    def test_a_missing_file_is_refused(self):
        response = self._client(self.admin).post(
            f"{self.logo_url}?tenant={self.tenant.slug}", {}, format="multipart",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_removing_the_logo_clears_it(self):
        self._upload(self.admin)
        response = self._delete(self.admin)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["logo"], "")

        self.school.refresh_from_db()
        self.assertFalse(self.school.branding.logo)

    def test_removing_a_logo_that_was_never_set_is_a_success(self):
        """Idempotent: there is nothing the caller would do differently on a 404."""
        response = self._delete(self.admin)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["logo"], "")

    def test_a_branch_admin_may_not_set_or_clear_the_logo(self):
        """Both verbs are writes. A read key must not reach either.

        This is the case the parent view's read/write split would have got
        wrong: DELETE is not a GET, but it is not a PATCH either, so the key had
        to be pinned on this view rather than inherited.
        """
        self.assertEqual(self._upload(self.branch_admin).status_code, 403)
        self.assertEqual(self._delete(self.branch_admin).status_code, 403)

    def test_setting_a_logo_is_recorded(self):
        """The screen tells the school every save is recorded. It must be true."""
        from vs_audit.models import AuditEvent

        self._upload(self.admin)
        event = (
            AuditEvent.objects
            .filter(entity_type="School", entity_id=str(self.school.pk))
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(event)
        self.assertIn("Logo updated", event.summary)
        self.assertEqual(event.actor_user, self.admin)
