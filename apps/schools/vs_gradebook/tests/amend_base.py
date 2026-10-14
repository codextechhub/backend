"""JSS1 A and Basic 3 published, with the people who raise and decide corrections.

Extends :class:`~.card_base.CardFixture` with two keys that only amend:
``amendment.create`` (ask for a correction) and ``amendment.approve`` (decide
one), each held on its own so a test can prove one opens nothing of the other,
and a role holding both so a test can prove nobody decides their own. Users are
school-wide (``amender``, ``approver``, ``both_keys``) or pinned to a branch
(``lekki_*``, ``ikeja_*``); Sunrise has its own pair.

JSS1 A is published with the default marks: Tunde 75, Chiamaka 64.5, Amina 60
(held) and Kelechi 40, positions 1 to 4. Mathematics is out of 100 as
20 + 20 + 60; Chiamaka and Tunde also take French. Kelechi's guardian has an
account, Chiamaka's an address, Tunde's neither.

:meth:`AmendmentFixture.cells` builds the body of a correction from three
marks, and :meth:`raise_amendment` raises one through the API.
"""
from __future__ import annotations

from unittest import mock
from urllib.parse import urlencode

from django.urls import reverse

from core.test_utils import TenantAPIClient

from ..constants import PERM_AMENDMENT_APPROVE, PERM_AMENDMENT_CREATE
from ..models import ClassTerm, StudentTermResult
from .base import make_key_role
from .card_base import SEND, CardFixture


class AmendmentFixture(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.create_role = make_key_role(
            cls.tenant, "Amendment Raiser", "amendment_raiser", (PERM_AMENDMENT_CREATE,),
        )
        cls.approve_role = make_key_role(
            cls.tenant, "Amendment Approver", "amendment_approver", (PERM_AMENDMENT_APPROVE,),
        )
        cls.both_role = make_key_role(
            cls.tenant, "Amendment Both", "amendment_both",
            (PERM_AMENDMENT_CREATE, PERM_AMENDMENT_APPROVE),
        )
        cls.amender = cls.make_user("amend@brightfield.test", "Amara", "Amend", cls.create_role)
        cls.lekki_amender = cls.make_user(
            "lekki.amend@brightfield.test", "Lade", "Amend", cls.create_role, branch=cls.lekki,
        )
        cls.ikeja_amender = cls.make_user(
            "ikeja.amend@brightfield.test", "Ire", "Amend", cls.create_role, branch=cls.ikeja,
        )
        cls.approver = cls.make_user(
            "approve@brightfield.test", "Apollo", "Approve", cls.approve_role,
        )
        cls.lekki_approver = cls.make_user(
            "lekki.approve@brightfield.test", "Lex", "Approve", cls.approve_role, branch=cls.lekki,
        )
        cls.ikeja_approver = cls.make_user(
            "ikeja.approve@brightfield.test", "Ife", "Approve", cls.approve_role, branch=cls.ikeja,
        )
        cls.both_keys = cls.make_user("both@brightfield.test", "Bola", "Both", cls.both_role)

        cls.solo_amender = cls.make_user(
            "amend@sunrise.test", "Sade", "Amend",
            make_key_role(cls.solo, "Amendment Raiser", "amendment_raiser", (PERM_AMENDMENT_CREATE,)),
            tenant=cls.solo,
        )
        cls.solo_approver = cls.make_user(
            "approve@sunrise.test", "Seun", "Approve",
            make_key_role(cls.solo, "Amendment Approver", "amendment_approver", (PERM_AMENDMENT_APPROVE,)),
            tenant=cls.solo,
        )

    #: Whether a test reads the real notification layer rather than a mock of it.
    real_notifications = False

    def setUp(self):
        super().setUp()
        if not self.real_notifications:
            patcher = mock.patch(SEND, return_value=["sent"])
            self.send = patcher.start()
            self.addCleanup(patcher.stop)

    # ── driving a class to published ──────────────────────────────────────

    @classmethod
    def published(cls, **kwargs):
        """JSS1 A published, Amina held; returns its class term."""
        kwargs.setdefault("held", [cls.amina])
        return cls.publish_jss1a(**kwargs)

    # ── bodies ────────────────────────────────────────────────────────────

    def cells(self, *marks):
        """The corrected cells of a request from up to three marks, in CA1, CA2, exam order.

        A mark that is None is left out, so ``cells(None, None, 50)`` corrects
        the examination only. A string is a marker (``"ABS"`` or ``"EXC"``).
        """
        out = []
        for component, mark in zip((self.ca1, self.ca2, self.exam), marks):
            if mark is None:
                continue
            body = {"component": component.pk, "mark": None, "marker": None}
            body["marker" if isinstance(mark, str) else "mark"] = mark
            out.append(body)
        return out

    def amendment_body(self, student, subject=None, cells=None, *, reason="Marked the paper wrongly.", **extra):
        return {
            "term": self.first_term.pk, "class": self.jss1a.pk, "student": student.pk,
            "subject": (subject or self.maths).pk, "reason": reason,
            "cells": cells if cells is not None else self.cells(None, None, 50),
            **extra,
        }

    # ── HTTP ──────────────────────────────────────────────────────────────

    def raise_amendment(self, user, student, subject=None, cells=None, **kwargs):
        """POST a correction; the keyword arguments override the body's keys."""
        body = self.amendment_body(student, subject, cells)
        body.update(kwargs)
        return self.post_amendment(user, body)

    @staticmethod
    def post_amendment(user, body):
        """POST *body* exactly as given to the raise route."""
        return TenantAPIClient(user).post(reverse("gradebook-amendments"), body, format="json")

    def amendment_form(self, user, student, subject=None, school_class=None, **query):
        query.setdefault("term", self.first_term.pk)
        query.setdefault("class", (school_class or self.jss1a).pk)
        query.setdefault("student", student.pk)
        query.setdefault("subject", (subject or self.maths).pk)
        return TenantAPIClient(user).get(f"{reverse('gradebook-amendment-form')}?{urlencode(query)}")

    def amendments_list(self, user, **query):
        query.setdefault("term", self.first_term.pk)
        return TenantAPIClient(user).get(f"{reverse('gradebook-amendments')}?{urlencode(query)}")

    def approve_amendment(self, user, amendment_id, **body):
        with self.captureOnCommitCallbacks(execute=True):
            return TenantAPIClient(user).post(
                reverse("gradebook-amendment-approve", kwargs={"pk": amendment_id}),
                body, format="json",
            )

    def reject_amendment(self, user, amendment_id, **body):
        return TenantAPIClient(user).post(
            reverse("gradebook-amendment-reject", kwargs={"pk": amendment_id}),
            body, format="json",
        )

    def raised(self, student, subject=None, cells=None, user=None, **kwargs):
        """Raise a correction through the API as *user* (the school-wide raiser) and return its id."""
        response = self.raise_amendment(user or self.amender, student, subject, cells, **kwargs)
        assert response.status_code == 201, response.data
        return self.data(response)["id"]

    def card(self, student, user=None):
        return self.data(self.report_card(user or self.releaser, student))

    # ── reading the stored state ──────────────────────────────────────────

    @staticmethod
    def result_of(student, school_class=None):
        return StudentTermResult.objects.get(
            student=student, **({"class_term__school_class": school_class} if school_class else {}),
        )

    def snapshots(self):
        """``{student_id: (version, snapshot)}`` for JSS1 A as stored now."""
        class_term = ClassTerm.objects.get(school_class=self.jss1a, term=self.first_term)
        return {
            row.student_id: (row.version, row.snapshot)
            for row in StudentTermResult.objects.filter(class_term=class_term)
        }

    @staticmethod
    def subject_of(snapshot, name):
        return next(row for row in snapshot["subjects"] if row["subject"]["name"] == name)
