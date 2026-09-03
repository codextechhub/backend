"""The logo a CodeX operator sees when they look at a school.

Every other surface already showed a school's logo - the sidebar, the sign-in
page, an invoice a parent opens - and the console showed none. Not because the
field was missing from the detail payload: it was there, carrying the bare
``/media/`` path an ``ImageField`` renders, which is refused twice over. Once
for having no signature, and then again by the tenant comparison in
``core.media.authorize``, which runs before any per-model policy and correctly
refuses a CodeX operator reading a row that belongs to Holy Cross.

So these tests pin the route rather than the field: the console is handed the
PUBLIC brand URL, which needs no session and crosses no tenant boundary,
because the same image is already painted on a sign-in page anybody can open.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.models import (
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.tests.helpers import make_permission, make_vision_user
from vs_user.tokens import CodeXRefreshToken

from .models import School, SchoolBranding

PNG = b"\x89PNG\r\n\x1a\n-logo-bytes"


def grant(user, *keys):
    role, _ = TenantRoleTemplate.objects.get_or_create(
        tenant=user.tenant, key=f"console-logo-{user.pk}",
        defaults={"name": f"Console Logo Role {user.pk}", "status": "ACTIVE"},
    )
    for key in keys:
        TenantRolePermission.objects.get_or_create(
            role=role, permission=make_permission(key),
        )
    TenantUserRoleAssignment.objects.get_or_create(
        tenant=user.tenant, user=user, role=role,
        defaults={"assignment_status": "ACTIVE"},
    )
    return role


class ConsoleSchoolLogoTests(TestCase):
    def setUp(self):
        self.operator = make_vision_user(email="logo-cx@codex.test")
        grant(self.operator, "platform.schools.view")
        self.client = APIClient()
        token = CodeXRefreshToken.for_user(self.operator).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def make_school(self, *, name="Holy Cross College", slug="holy-cross", logo=PNG):
        school = School.objects.create(name=name, slug=slug, code=slug.upper()[:8],
                                       status="ACTIVE")
        branding = SchoolBranding(school=school)
        if logo:
            branding.logo = SimpleUploadedFile(f"{slug}.png", logo,
                                               content_type="image/png")
        branding.save()
        return school

    def get(self, url):
        joiner = "&" if "?" in url else "?"
        return self.client.get(f"{url}{joiner}tenant={self.operator.tenant.slug}")

    # ── the detail screen ────────────────────────────────────────────────────

    def test_detail_hands_the_console_a_url_it_can_actually_fetch(self):
        """The defect in one test: the old payload's URL answered 404."""
        school = self.make_school()
        response = self.get(reverse("school-detail", args=[school.slug]))
        self.assertEqual(response.status_code, 200, response.content[:200])

        logo = response.json()["data"]["branding"]["logo"]
        self.assertTrue(logo, "the console was given no logo at all")
        self.assertNotIn("/media/", logo,
                         "a /media/ path is refused across tenants, whatever it is signed with")

        # Fetched with no credentials whatsoever, which is what an <img> does.
        image = APIClient().get(logo.replace("http://testserver", ""))
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.content, PNG)

    def test_detail_says_nothing_when_the_school_has_no_logo(self):
        school = self.make_school(slug="bright-star", name="Bright Star", logo=b"")
        response = self.get(reverse("school-detail", args=[school.slug]))

        self.assertEqual(response.json()["data"]["branding"]["logo"], "")

    # ── the list screen ──────────────────────────────────────────────────────

    def test_the_list_carries_a_logo_per_row(self):
        self.make_school()
        self.make_school(slug="bright-star", name="Bright Star", logo=b"")
        response = self.get(reverse("school-list"))
        self.assertEqual(response.status_code, 200, response.content[:200])

        rows = {r["slug"]: r["logo"] for r in response.json()["data"]}
        self.assertIn("holy-cross", rows)
        self.assertTrue(rows["holy-cross"])
        # Empty, not null: a row with no logo and a row whose logo the reader
        # may not fetch must reach the same fallback without the screen asking
        # which it was.
        self.assertEqual(rows["bright-star"], "")

    def test_the_logo_column_does_not_cost_a_query_per_school(self):
        """The list select_related's branding for exactly this.

        Compared against itself at two sizes rather than pinned to a literal:
        the count that matters is the one that GROWS, and a fixed number would
        fail on any unrelated change to the list while saying nothing about
        this one. One school and seven schools must cost the same.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        url = f"{reverse('school-list')}?tenant={self.operator.tenant.slug}"

        self.make_school()
        with CaptureQueriesContext(connection) as one_school:
            self.assertEqual(len(self.client.get(url).json()["data"]), 1)

        for i in range(6):
            self.make_school(slug=f"school-{i}", name=f"School {i}")
        with CaptureQueriesContext(connection) as seven_schools:
            self.assertEqual(len(self.client.get(url).json()["data"]), 7)

        # Flat. Seven schools cost what one costs.
        #
        # This read 1 until ``School.main_branch`` learned to use the prefetched
        # branches: it chained ``.select_related().filter()`` onto the manager,
        # which builds a fresh queryset and so could never use the rows the view
        # had already paid to fetch. Both costs are pinned by the same number,
        # so whichever of them regresses, this fails.
        growth = (len(seven_schools) - len(one_school)) / 6
        self.assertEqual(
            growth, 0,
            "the school list grew a query per row: branding is no longer "
            "joined, main_branch is re-querying, or a serializer is reaching "
            "past the row it was given",
        )
