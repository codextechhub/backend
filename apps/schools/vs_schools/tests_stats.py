"""``GET /v1/i/stats/`` - the School Management stat cards.

The endpoint had no tests, and it was missing a count: SUSPENDED was added to
:class:`SchoolStatus` after the aggregate was written, and the aggregate named
its statuses by hand. The tab whose figure was missing fetched its own count
with a second request, which worked and hid the gap.

So these tests pin the *shape* rather than four particular keys. A status added
to the enum and not to the response is the failure they exist to catch, and
that failure is silent everywhere else.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import make_school, make_vision_user

from .models import SchoolStatus

VIEW_KEY = "platform.schools.view"


class SchoolStatsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = make_vision_user(email="stats@example.com", super_admin=True)

    def _get(self, *, expect=200, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.user)
        response = client.get(reverse("school-stats"))
        self.assertEqual(response.status_code, expect, response.data)
        return response

    def _make(self, slug, status):
        school = make_school(slug=slug, name=slug.title())
        school.status = status
        school.save()
        return school

    def test_a_caller_without_the_key_is_refused(self):
        self._get(expect=403, user=make_vision_user(email="nokey-stats@example.com"))

    def test_every_status_in_the_enum_has_a_count(self):
        """The regression test. Naming statuses by hand is what dropped
        SUSPENDED, so the assertion is against the enum, not a list."""
        data = self._get().data["data"]

        for value in SchoolStatus.values:
            self.assertIn(value.lower(), data, f"{value} has no count")

    def test_suspended_is_counted(self):
        self._make("abandoned-one", SchoolStatus.SUSPENDED)
        self._make("abandoned-two", SchoolStatus.SUSPENDED)
        self._make("running", SchoolStatus.ACTIVE)

        data = self._get().data["data"]

        self.assertEqual(data["suspended"], 2)

    def test_the_counts_are_by_status_and_all_is_the_total(self):
        self._make("live-one", SchoolStatus.ACTIVE)
        self._make("onboarding", SchoolStatus.PENDING)
        self._make("closed", SchoolStatus.INACTIVE)
        self._make("expired", SchoolStatus.SUSPENDED)

        data = self._get().data["data"]

        self.assertEqual(data["active"], 1)
        self.assertEqual(data["pending"], 1)
        self.assertEqual(data["inactive"], 1)
        self.assertEqual(data["suspended"], 1)
        self.assertEqual(
            data["all"],
            sum(data[v.lower()] for v in SchoolStatus.values),
            "all must account for every school, whatever its status",
        )

    def test_the_keys_the_frontend_already_reads_are_unchanged(self):
        """Deriving the keys must not rename the four that shipped."""
        data = self._get().data["data"]

        for key in ("all", "active", "pending", "inactive"):
            self.assertIn(key, data)
