"""What a parent's phone installs: the parent app under the school's own name and crest.

Corona Secondary School has uploaded its crest, so a parent's home screen shows
"Corona" with icons cut from it, opening the parent app at ``/parent/``, not the
student app. Lagoon View Academy has none and keeps the parent app's own XVS
icons. A suspended school answers nothing. Nobody needs to be signed in: the
phone asks before anybody has.
"""
from __future__ import annotations

import io

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from schools.vs_schools.models import School, SchoolBranding
from schools.vs_student_portal.tests.test_app_manifest import png


class ParentAppManifestTests(TestCase):
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
        return self.client.get(reverse("parent-app-manifest", args=[slug]))

    def test_a_school_with_a_crest_is_installed_as_the_parent_app_under_its_name(self):
        response = self.manifest("corona")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        body = response.json()
        self.assertEqual((body["name"], body["short_name"]), ("Corona Secondary School", "Corona"))
        self.assertEqual(
            (body["id"], body["start_url"], body["scope"]), ("/parent/", "/parent/", "/parent/"),
        )
        self.assertIn("children", body["description"])
        icon = next(i for i in body["icons"] if i["purpose"] == "maskable")
        manifest_url = reverse("parent-app-manifest", args=["corona"])
        self.assertEqual(
            manifest_url.rsplit("/", 1)[0] + "/" + icon["src"],
            reverse("parent-app-icon", args=["corona", "maskable-512.png"]),
        )
        self.assertEqual(body["related_applications"], [{"platform": "webapp", "url": "manifest.webmanifest"}])

    def test_the_student_app_keeps_its_own_address(self):
        body = self.client.get(reverse("student-app-manifest", args=["corona"])).json()
        self.assertEqual((body["id"], body["start_url"]), ("/student/", "/student/"))

    def test_icons_are_drawn_from_the_crest(self):
        response = self.client.get(reverse("parent-app-icon", args=["corona", "icon-512.png"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Image.open(io.BytesIO(response.content)).size, (512, 512))

    def test_a_school_without_a_crest_keeps_the_parent_apps_own_icons(self):
        body = self.manifest("lagoon-view").json()
        self.assertTrue(all(i["src"].startswith("/parent/icons/") for i in body["icons"]))
        response = self.client.get(reverse("parent-app-icon", args=["lagoon-view", "icon-192.png"]))
        self.assertEqual(response.status_code, 404)

    def test_a_school_that_cannot_sign_in_answers_nothing(self):
        School.objects.create(name="Closed College", slug="closed", status="SUSPENDED")
        self.assertEqual(self.manifest("closed").status_code, 404)
        self.assertEqual(self.manifest("nobody").status_code, 404)
