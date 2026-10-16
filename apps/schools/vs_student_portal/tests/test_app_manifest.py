"""What a phone installs: the student app under the school's own name and crest.

Corona Secondary School has uploaded its crest, so its pupils' home screens
show "Corona" with icons cut from it. Lagoon View Academy has none and keeps
the app's own XVS icons. A suspended school answers nothing.
"""
from __future__ import annotations

import io

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from schools.vs_schools.models import School, SchoolBranding

from ..app_manifest import short_name


def png(width, height, colour=(200, 30, 30, 255)):
    out = io.BytesIO()
    Image.new("RGBA", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


class ShortNameTests(SimpleTestCase):
    def test_whole_words_that_fit_under_an_icon(self):
        self.assertEqual(short_name("Corona Secondary School"), "Corona")
        self.assertEqual(short_name("Lagoon View Academy"), "Lagoon View")
        self.assertEqual(short_name("Ogbomosholand College"), "Ogbomosholan")


class StudentAppManifestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.corona = School.objects.create(name="Corona Secondary School", slug="corona", status="ACTIVE")
        branding = SchoolBranding(school=cls.corona)
        branding.logo = SimpleUploadedFile("corona.png", png(300, 200), content_type="image/png")
        branding.save()
        cls.lagoon = School.objects.create(name="Lagoon View Academy", slug="lagoon-view", status="ACTIVE")
        SchoolBranding.objects.create(school=cls.lagoon)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def manifest(self, slug):
        return self.client.get(reverse("student-app-manifest", args=[slug]))

    def test_a_school_with_a_crest_is_installed_under_its_name_and_crest(self):
        response = self.manifest("corona")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        body = response.json()
        self.assertEqual((body["name"], body["short_name"]), ("Corona Secondary School", "Corona"))
        self.assertEqual((body["start_url"], body["scope"]), ("/student/", "/student/"))
        icon = next(i for i in body["icons"] if i["purpose"] == "maskable")
        self.assertEqual(icon["src"], "icons/maskable-512.png")
        # Relative to the manifest, it is the icon route, whatever host served it.
        manifest_url = reverse("student-app-manifest", args=["corona"])
        self.assertEqual(
            manifest_url.rsplit("/", 1)[0] + "/" + icon["src"],
            reverse("student-app-icon", args=["corona", "maskable-512.png"]),
        )

    def test_icons_are_drawn_from_the_crest_at_the_size_asked(self):
        for name, size in (("icon-192.png", 192), ("maskable-512.png", 512), ("apple-touch-icon.png", 180)):
            with self.subTest(name=name):
                response = self.client.get(reverse("student-app-icon", args=["corona", name]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(Image.open(io.BytesIO(response.content)).size, (size, size))

    def test_a_school_without_a_crest_keeps_the_apps_own_icons(self):
        body = self.manifest("lagoon-view").json()
        self.assertEqual(body["short_name"], "Lagoon View")
        self.assertTrue(all(i["src"].startswith("/student/icons/") for i in body["icons"]))
        response = self.client.get(reverse("student-app-icon", args=["lagoon-view", "icon-192.png"]))
        self.assertEqual(response.status_code, 404)

    def test_a_school_that_cannot_sign_in_answers_nothing(self):
        School.objects.create(name="Closed College", slug="closed", status="SUSPENDED")
        self.assertEqual(self.manifest("closed").status_code, 404)
        self.assertEqual(self.manifest("nobody").status_code, 404)

    def test_an_unknown_icon_name_is_not_found(self):
        response = self.client.get(reverse("student-app-icon", args=["corona", "favicon.ico"]))
        self.assertEqual(response.status_code, 404)
