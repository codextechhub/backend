"""The crest a sign-in page can read before anyone has signed in.

The route exists because every other path to a school's logo needs a session or
a pay token, and the sign-in page has neither. It is a deliberate, narrow piece
of public surface, so most of what is tested here is what it refuses to say.
"""
from __future__ import annotations

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_tenants.models import Tenant

from .models import School, SchoolBranding

PNG = b"\x89PNG\r\n\x1a\n-crest-bytes"


class PublicSchoolLogoTests(TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def make_school(self, *, name="Holy Cross College", slug="holy-cross",
                    logo=PNG, status="ACTIVE"):
        school = School.objects.create(name=name, slug=slug, status=status)
        branding = SchoolBranding(school=school)
        if logo:
            branding.logo = SimpleUploadedFile(
                f"{slug}.png", logo, content_type="image/png",
            )
        branding.save()
        return school

    def url(self, slug):
        return reverse("public-school-logo", args=[slug])

    def test_a_school_crest_is_served_without_any_session(self):
        self.make_school()
        response = self.client.get(self.url("holy-cross"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PNG)
        self.assertTrue(response["Content-Type"].startswith("image/"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_it_may_be_cached_publicly(self):
        # Unlike the pay-link crest, this one is painted on a page anyone can
        # open, so a shared cache holding it is the point rather than a leak.
        self.make_school()
        self.assertIn("public", self.client.get(self.url("holy-cross"))["Cache-Control"])

    def test_the_slug_is_case_insensitive(self):
        self.make_school()
        self.assertEqual(self.client.get(self.url("Holy-Cross")).status_code, 200)

    # -- what it refuses to distinguish ---------------------------------------- #

    def test_a_school_with_no_crest_and_a_slug_that_is_not_a_school_look_alike(self):
        """The whole enumeration argument rests on this.

        If a real school without a logo answered differently from a slug nobody
        has taken, walking a word list would map Codex's customers. Both are
        404, and the bodies match.
        """
        self.make_school(name="St. Monica's Academy", slug="st-monicas", logo=b"")

        real = self.client.get(self.url("st-monicas"))
        invented = self.client.get(self.url("no-such-school"))

        self.assertEqual(real.status_code, 404)
        self.assertEqual(invented.status_code, 404)
        self.assertEqual(real.content, invented.content)

    def test_a_tenant_that_cannot_sign_in_cannot_be_probed(self):
        # Same lookup sign-in performs. A suspended school is not answerable
        # here for the same reason it is not answerable at the door.
        school = self.make_school()
        Tenant.objects.filter(pk=school.tenant_id).update(status=Tenant.Status.SUSPENDED)

        self.assertEqual(self.client.get(self.url("holy-cross")).status_code, 404)

    def test_the_platform_tenant_is_not_a_school(self):
        self.assertEqual(self.client.get(self.url("codex")).status_code, 404)

    def test_one_school_slug_never_serves_another_school_crest(self):
        # The file is chosen by the slug's own branding row, so there is no
        # reference here for a caller to point somewhere else.
        self.make_school()
        other = b"\x89PNG\r\n\x1a\n-bright-star"
        self.make_school(name="Bright Star School", slug="bright-star", logo=other)

        self.assertEqual(self.client.get(self.url("holy-cross")).content, PNG)
        self.assertEqual(self.client.get(self.url("bright-star")).content, other)

    def test_it_takes_no_session_and_answers_the_same_with_one(self):
        # authentication_classes is empty, so a stale token on a shared browser
        # cannot change what this route does.
        self.make_school()
        anonymous = self.client.get(self.url("holy-cross"))
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
        with_token = self.client.get(self.url("holy-cross"))

        self.assertEqual(anonymous.status_code, with_token.status_code)
        self.assertEqual(anonymous.content, with_token.content)
