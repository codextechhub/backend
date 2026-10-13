"""JSS1 A published, with the people who read its release list and report cards.

Extends :class:`~.class_base.ClassFixture` with two keys that only read:
``release.view`` (the release list and the cards) and ``result.view`` (a
student's Results tab), each held on its own so a test can prove one key opens
nothing of the other. ``releaser`` and ``result_reader`` are school-wide;
``lekki_*`` and ``ikeja_*`` are pinned to a branch; Sunrise has its own pair.

:meth:`CardFixture.publish_jss1a` drives JSS1 A through the real services:
Tunde, Chiamaka, Amina and Kelechi are published with the default marks (Tunde
75, Chiamaka 64.5, Amina 60, Kelechi 40, positions 1 to 4), Tunde's mother has no
address (``NO_EMAIL``), Chiamaka's and Mrs Eze's emails are sent (``SENT``) and
Amina, held by the bursar, is ``HELD``. Tunde has ratings. The attendance
summary is a fixed fake and the notification layer is mocked, as in the
publishing tests, so no email leaves the test.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_attendance.services.term_summary import StudentTermCounts
from schools.vs_gradebook.models import ClassTerm, ResultHold, StudentTermResult
from schools.vs_gradebook.services import publishing

from ..constants import PERM_RELEASE_VIEW, PERM_RESULT_VIEW, SheetStatus
from .base import make_key_role, school_clock
from .class_base import ClassFixture

SEND = "schools.vs_gradebook.services.publishing.send_notification"
SUMMARY = "schools.vs_gradebook.services.publishing.class_term_summary"

#: Tuesday 16 December 2025, 09:00 UTC: the day a release is shown as.
RELEASED_AT = dt.datetime(2025, 12, 16, 9, 0, tzinfo=dt.timezone.utc)
TUNDE_RATINGS = {"Punctuality": 5, "Sports": 3}


def counts_for(*students):
    return SimpleNamespace(days_not_taken=0, students={
        student.pk: StudentTermCounts(opened=60, present=55, absent=5, late=2, excused=0, not_taken=0)
        for student in students
    })


class CardFixture(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.release_role = make_key_role(cls.tenant, "Release Reader", "release_reader", (PERM_RELEASE_VIEW,))
        cls.result_role = make_key_role(cls.tenant, "Result Reader", "result_reader", (PERM_RESULT_VIEW,))
        cls.releaser = cls.make_user("release@brightfield.test", "Rena", "Release", cls.release_role)
        cls.lekki_releaser = cls.make_user(
            "lekki.release@brightfield.test", "Lex", "Release", cls.release_role, branch=cls.lekki,
        )
        cls.ikeja_releaser = cls.make_user(
            "ikeja.release@brightfield.test", "Ike", "Release", cls.release_role, branch=cls.ikeja,
        )
        cls.result_reader = cls.make_user("result@brightfield.test", "Reza", "Result", cls.result_role)
        cls.lekki_result_reader = cls.make_user(
            "lekki.result@brightfield.test", "Lola", "Result", cls.result_role, branch=cls.lekki,
        )
        cls.ikeja_result_reader = cls.make_user(
            "ikeja.result@brightfield.test", "Ivy", "Result", cls.result_role, branch=cls.ikeja,
        )
        cls.solo_releaser = cls.make_user(
            "release@sunrise.test", "Sam", "Release",
            make_key_role(cls.solo, "Release Reader", "release_reader", (PERM_RELEASE_VIEW,)),
            tenant=cls.solo,
        )
        cls.solo_result_reader = cls.make_user(
            "result@sunrise.test", "Sade", "Result",
            make_key_role(cls.solo, "Result Reader", "result_reader", (PERM_RESULT_VIEW,)),
            tenant=cls.solo,
        )

    # ── driving a class to published ──────────────────────────────────────

    @classmethod
    def publish_jss1a(cls, *, held=(), ratings=True, **marks):
        """JSS1 A published by the principal, the committed emails sent; returns its class term."""
        cls.drive_to_awaiting(**marks)
        if ratings:
            StudentTermResult.objects.filter(student=cls.tunde).update(ratings=TUNDE_RATINGS)
        for student in held:
            ResultHold.objects.create(
                tenant=cls.tenant, term=cls.first_term, student=student,
                reason="Fees outstanding since September.", placed_by=cls.bursar,
            )
        everyone = (cls.chiamaka, cls.tunde, cls.kelechi, cls.amina)
        with school_clock(), mock.patch(SUMMARY, return_value=counts_for(*everyone)):
            response = TenantAPIClient(cls.principal).post(
                reverse("gradebook-publish") + f"?term={cls.first_term.pk}",
                {"classes": [cls.jss1a.pk]}, format="json",
            )
        assert response.status_code == 200, response.data
        class_term = ClassTerm.objects.get(school_class=cls.jss1a, term=cls.first_term)
        StudentTermResult.objects.filter(
            class_term=class_term, released_at__isnull=False,
        ).update(released_at=RELEASED_AT)
        pending = list(
            StudentTermResult.objects.filter(
                class_term=class_term, released_at__isnull=False, email_state__isnull=True,
            ).values_list("student_id", flat=True)
        )
        with mock.patch(SEND, return_value=["sent"]):
            publishing.send_ready_notices(cls.tenant, class_term.pk, pending)
        return class_term

    @classmethod
    def publish_sunrise(cls):
        """Sunrise's Basic 3 published: it ranks nobody and records no ratings."""
        cls.make_sheet(
            cls.solo_class, cls.solo_maths, status=SheetStatus.SUBMITTED,
            rows={cls.solo_pupil: (18, 15, 46), cls.solo_pupil_two: (10, 10, 20)},
        )
        client = TenantAPIClient(cls.solo_teacher)
        query = f"?term={cls.solo_term.pk}"
        with school_clock(), mock.patch(
            SUMMARY, return_value=counts_for(cls.solo_pupil, cls.solo_pupil_two),
        ):
            for pupil in (cls.solo_pupil, cls.solo_pupil_two):
                client.put(
                    reverse(
                        "gradebook-compile",
                        kwargs={"class_id": cls.solo_class.pk, "student_id": pupil.pk},
                    ) + query, {"teacher_comment": "Good."}, format="json",
                )
            client.post(
                reverse("gradebook-class-submit", kwargs={"class_id": cls.solo_class.pk}) + query,
                {}, format="json",
            )
            response = TenantAPIClient(cls.solo_principal).post(
                reverse("gradebook-publish") + query, {"classes": [cls.solo_class.pk]},
                format="json",
            )
        assert response.status_code == 200, response.data

    # ── HTTP ──────────────────────────────────────────────────────────────

    def release(self, user, school_class=None, **kwargs):
        return self.call(
            "get", user, "gradebook-release", class_id=(school_class or self.jss1a).pk,
            term=kwargs.pop("term", self.first_term), **kwargs,
        )

    def report_card(self, user, student, school_class=None, **kwargs):
        return self.call(
            "get", user, "gradebook-report-card", class_id=(school_class or self.jss1a).pk,
            student_id=student.pk, term=kwargs.pop("term", self.first_term), **kwargs,
        )

    def class_cards(self, user, school_class=None, **kwargs):
        return self.call(
            "get", user, "gradebook-report-cards", class_id=(school_class or self.jss1a).pk,
            term=kwargs.pop("term", self.first_term), **kwargs,
        )

    def results_tab(self, user, student):
        return self.call("get", user, "gradebook-student-results", student_id=student.pk)

    @staticmethod
    def lines(card, key):
        """A card's ``{label, value}`` lines as a dictionary."""
        return {line["label"]: line["value"] for line in card[key]}
