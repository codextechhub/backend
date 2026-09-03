"""The three mounts, and the ordering trap that would 404 all of them.

``vs_academics`` mounts at ``v1/academics/``. Django tries patterns in list
order, so an entry for the shorter prefix placed first matches
``v1/academics/timetable/rooms/``, fails to resolve the remainder inside that
module's urlconf, and answers 404
rather than falling through to this module. Three lines of care in
``apps/urls.py``, and a test that is cheaper than finding out from a customer.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import resolve

from ..views.events import EventListCreateView
from ..views.exams import ExamListCreateView
from ..views.rooms import RoomListCreateView


class MountOrderTests(SimpleTestCase):
    def test_the_timetable_prefix_reaches_this_module(self):
        match = resolve("/v1/academics/timetable/rooms/")
        self.assertIs(match.func.cls, RoomListCreateView)

    def test_the_calendar_prefix_reaches_this_module(self):
        match = resolve("/v1/academics/calendar/events/")
        self.assertIs(match.func.cls, EventListCreateView)

    def test_the_exams_prefix_reaches_this_module(self):
        match = resolve("/v1/academics/exams/")
        self.assertIs(match.func.cls, ExamListCreateView)

    def test_m13s_own_routes_still_resolve_to_m13(self):
        """Nothing here may shadow the session and term routes."""
        match = resolve("/v1/academics/sessions/")
        self.assertEqual(match.func.cls.__module__.split(".")[1], "vs_academics")


class PendingSurfaceTests(SimpleTestCase):
    def test_every_view_in_this_module_declares_the_pending_surface(self):
        """A school builds its calendar and bell schedule before it goes live.

        Absence means closed, deliberately, so a view added later is not
        admitted by default - which is why this enumerates the URL conf rather
        than trusting the base class.
        """
        from django.urls import get_resolver

        missing = []
        for pattern in get_resolver().url_patterns:
            for entry in _walk(pattern):
                view = getattr(entry.callback, "cls", None)
                if view is None:
                    continue
                if not view.__module__.startswith("schools.vs_calendar"):
                    continue
                if not getattr(view, "pending_tenant_surface", False):
                    missing.append(view.__name__)
        self.assertEqual(missing, [], f"not declared: {missing}")


def _walk(pattern):
    if hasattr(pattern, "url_patterns"):
        for child in pattern.url_patterns:
            yield from _walk(child)
    elif hasattr(pattern, "callback"):
        yield pattern


class NoGenerateEndpointTests(SimpleTestCase):
    def test_there_is_no_auto_generate_route_of_any_kind(self):
        """Absent rather than present and disabled.

        A generator needs three facts the platform does not record: how many
        periods a week each subject should get, when each teacher is available,
        and what each teacher actually teaches. Without them it would produce an
        arbitrary grid wearing the authority of a machine. A builder should not
        find a stub here to fill in.
        """
        from django.urls import get_resolver

        for pattern in get_resolver().url_patterns:
            for entry in _walk(pattern):
                view = getattr(entry.callback, "cls", None)
                if view is None or not view.__module__.startswith(
                    "schools.vs_calendar",
                ):
                    continue
                self.assertNotIn("generate", str(entry.pattern).lower())
                self.assertNotIn("suggest", str(entry.pattern).lower())
