"""Regression coverage for the admin live-session search contract."""

from django.test import TestCase
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    make_branch,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_user.models import LoginSession


class LiveSessionSearchTests(TestCase):
    def setUp(self):
        self.viewer = make_vision_user(
            email="session.viewer@codex.test",
            super_admin=True,
        )
        self.school = make_school(
            slug="search-academy",
            name="Search Academy",
        )
        self.matching_user = make_school_admin(
            make_branch(self.school),
            email="ada.session@codex.test",
            first_name="Ada",
            last_name="Lovelace",
        )
        self.other_user = make_vision_user(
            email="grace.session@codex.test",
            first_name="Grace",
            last_name="Hopper",
        )
        self.matching = LoginSession.objects.create(
            user=self.matching_user,
            tenant=self.matching_user.tenant,
            ip_address="203.0.113.42",
            device_label="Ada's work laptop",
            user_agent="Mozilla/5.0 SearchBrowser/9.1",
        )
        LoginSession.objects.create(
            user=self.other_user,
            tenant=self.other_user.tenant,
            ip_address="198.51.100.17",
            device_label="Grace's tablet",
            user_agent="Mozilla/5.0 OtherBrowser/1.0",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.viewer)

    def _returned_ids(self, search):
        response = self.client.get(
            "/v1/user/sessions/",
            {"search": search, "page_size": 100},
        )
        self.assertEqual(response.status_code, 200, response.content)
        return {item["id"] for item in response.json()["data"]}

    def test_search_matches_the_fields_shown_on_the_live_sessions_screen(self):
        for search in (
            "ada.session",
            "Ada Lovelace",
            "203.0.113.42",
            "work laptop",
            "SearchBrowser",
            "Search Academy",
        ):
            with self.subTest(search=search):
                self.assertEqual(self._returned_ids(search), {self.matching.id})

    def test_search_excludes_sessions_that_do_not_match(self):
        self.assertEqual(self._returned_ids("no such live session"), set())
