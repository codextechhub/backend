"""What the payloads look like, how many queries they cost, and who reaches them.

Three things the ship-check asks for by name and that nothing else here covers:
the empty shape, the query count, and whether a school still onboarding actually
reaches the surfaces it is supposed to.

FRD M12 v2.1 sections 12.1 and 12.5, and FR-018.
"""
from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.models import StaffProfile
from vs_tenants.models import Tenant

from .base import StaffFixture


class EmptyShapeTests(StaffFixture):
    """An empty list stays a list.

    ``success_response`` returns ``{}`` only for a genuinely absent payload, and
    a caller doing ``data.map(...)`` on the first day of a school with one person
    in it must not crash. Asserted explicitly on every list this module serves,
    because the shape is easy to change by accident and impossible to notice
    until a real school opens the screen.
    """

    def test_an_empty_qualification_list_is_a_list(self):
        response = self.get(self.admin, "staff-qualifications", pk=self.eze.pk)
        self.assertEqual(response.data["data"], [])

    def test_an_empty_document_list_is_a_list(self):
        response = self.get(self.admin, "staff-documents", pk=self.eze.pk)
        self.assertEqual(response.data["data"], [])

    def test_an_empty_search_is_a_list(self):
        response = self.get(self.admin, "staff-search", {"q": "zzzz"})
        self.assertEqual(response.data["data"], [])

    def test_an_empty_leave_list_is_a_list(self):
        response = self.get(self.admin, "staff-leave", pk=self.eze.pk)
        self.assertEqual(response.data["data"]["leave"], [])

    def test_an_empty_history_is_a_list(self):
        bare = self.make_staff(
            "bare@brightfield.test", "Bare", "Record", branch=self.lekki,
        )
        bare.employment_events.all().delete()
        response = self.get(self.admin, "staff-history", pk=bare.pk)
        self.assertEqual(response.data["data"]["entries"], [])

    def test_a_person_with_no_roles_gets_an_empty_list_not_a_null(self):
        bare = self.make_staff(
            "noroles@brightfield.test", "No", "Roles", branch=self.lekki,
        )
        response = self.get(self.admin, "staff-roles", pk=bare.pk)
        self.assertEqual(response.data["data"]["roles"], [])


class QueryCountTests(StaffFixture):
    """A page of people must not be a page of queries.

    The endpoint this replaced carried the measurement in its own docstring: a
    page of twenty-five without the prefetches was fifty extra queries, one per
    person for their roles and one for their invitation. The bound below is
    deliberately generous, because the point is that the cost does not grow with
    the number of people, not that it hits an exact number a refactor would
    have to chase.
    """

    #: Roughly: the page, the count, the prefetches, the aggregates, and the
    #: request's own tenant and permission lookups.
    CEILING = 30

    def setUp(self):
        super().setUp()
        for index in range(25):
            self.make_staff(
                f"person{index}@brightfield.test", "Person", f"Number{index}",
                branch=self.lekki if index % 2 else None,
            )

    def test_a_page_of_twenty_five_is_a_bounded_number_of_queries(self):
        client = self.client_for(self.admin)
        with CaptureQueriesContext(connection) as captured:
            response = client.get(
                "/v1/i/me/staff/", {"tenant": self.tenant.slug, "page_size": 25},
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertLessEqual(
            len(captured), self.CEILING,
            f"{len(captured)} queries for one page. The prefetches on the "
            f"queryset are what keep this flat; check none was dropped.",
        )

    def test_the_cost_does_not_grow_with_the_roll(self):
        """The assertion that actually catches an N+1.

        A fixed ceiling can be met by a page that happens to be small. Comparing
        five people against twenty-five is what shows the count is flat.
        """
        client = self.client_for(self.admin)

        def cost(page_size):
            with CaptureQueriesContext(connection) as captured:
                client.get(
                    "/v1/i/me/staff/",
                    {"tenant": self.tenant.slug, "page_size": page_size},
                )
            return len(captured)

        self.assertLessEqual(cost(25), cost(5) + 2)


class PendingTenantSurfaceTests(StaffFixture):
    """A school that has not gone live, reaching real endpoints.

    ``test_urls.py`` asserts which views DECLARE the attribute, which is the
    mechanism. This asserts what a PENDING school actually gets, which is the
    outcome, and the two are worth having separately: a view could declare the
    attribute and still be unreachable for some other reason, and a school
    unable to finish onboarding is the failure that matters.
    """

    def setUp(self):
        super().setUp()
        self.tenant.status = Tenant.Status.PENDING
        self.tenant.save(update_fields=["status"])

    def test_a_pending_school_can_list_its_staff(self):
        response = self.get(self.admin, "staff-list")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_pending_school_can_read_one_record(self):
        response = self.get(self.admin, "staff-detail", pk=self.eze.pk)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_pending_school_can_move_a_posting(self):
        """Part of setting itself up, so it has to work before go-live."""
        response = self.post(
            self.admin, "staff-bulk-posting",
            {"staff_ids": [self.eze.pk], "branch": self.ikeja.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_pending_school_is_refused_the_lifecycle(self):
        """Nobody resigns during onboarding, and the key is SENSITIVE."""
        response = self.post(
            self.admin, "staff-status",
            {"to_status": "SUSPENDED", "reason": "Pending a review."},
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_pending_school_is_refused_teaching(self):
        response = self.get(self.admin, "staff-teaching", pk=self.eze.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_pending_school_is_refused_leave(self):
        response = self.get(self.admin, "staff-leave", pk=self.eze.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_pending_school_is_refused_coverage(self):
        response = self.get(self.admin, "staff-teaching-coverage")
        self.assertEqual(response.status_code, 403, response.data)

    def test_the_refusal_is_a_distinct_code_the_frontend_can_word_as_not_yet(self):
        """Not-yet and not-allowed are different sentences to a school."""
        response = self.get(self.admin, "staff-leave", pk=self.eze.pk)
        self.assertIn("TENANT_NOT_LIVE", str(response.data))

    def test_going_live_opens_the_closed_surfaces_with_no_further_action(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["status"])
        response = self.get(self.admin, "staff-teaching", pk=self.eze.pk)
        self.assertEqual(response.status_code, 200, response.data)
