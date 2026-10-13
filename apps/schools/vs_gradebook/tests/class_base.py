"""Brightfield and Sunrise with a class that can be driven from sheets to a published term.

Extends :class:`~.sheet_base.SheetFixture` with the people who work a class after
its sheets are in: a principal (``class.publish``), a bursar (the fee-hold keys
and nothing else), the guardians a report card is emailed to, and a helper that
brings JSS1 A to the point a test needs through the real services.

**What the fixture adds**

* Users: ``principal`` (school-wide), ``lekki_principal`` and ``ikeja_principal``
  (pinned to one branch), ``bursar`` (``hold.view`` and ``hold.update``),
  ``lekki_bursar`` and ``ikeja_bursar`` (pinned), ``hold_viewer`` (``hold.view``
  only). Sunrise has its own ``solo_principal`` and ``solo_bursar``.
* JSS1 A's roll on the 13th is Tunde, Kelechi, Chiamaka and Amina (suspended), in
  name order. Its two sheets are Mathematics (everybody) and French (Chiamaka and
  Tunde). The default marks give averages of Tunde 75, Chiamaka 64.5, Amina 60
  and Kelechi 40, so positions 1 to 4, in the bands 70, 55, 55 and 40 of the
  remark templates.
* Guardians: Chiamaka's mother has an address. Tunde's mother has none. Kelechi
  and Amina are sisters-and-brothers who share one primary guardian, Mrs Eze,
  who has an account, so a publication writes to her twice.
* Sunrise's two pupils each have a guardian with an address; Sunrise ranks
  nobody and records no conduct ratings.
* The FAL's finance reader is a fake (:meth:`finance`) that a test sets and that
  is put back afterwards.
"""
from __future__ import annotations

import datetime as dt

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.core.fal import registry
from schools.core.fal.contracts import AgeingBucket, DebtorRow
from schools.core.fal.testing import FakeFinanceReader, unavailable_finance_reader
from schools.vs_students.constants import Relationship
from schools.vs_students.models import Guardian, StudentGuardian
from vs_rbac.tests.helpers import make_school_admin

from ..constants import (
    CFG_CONDUCT_RATINGS,
    CFG_RANK_IN_CLASS,
    PERM_CLASS_PUBLISH,
    PERM_HOLD_UPDATE,
    PERM_HOLD_VIEW,
    SheetStatus,
)
from .base import make_key_role, school_clock, set_setting
from .sheet_base import SheetFixture

PRINCIPAL_KEYS = (PERM_CLASS_PUBLISH,)
BURSAR_KEYS = (PERM_HOLD_VIEW, PERM_HOLD_UPDATE)

SENT_COMMENT = "A pleasant, hardworking student."


class ClassFixture(SheetFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.principal_role = make_key_role(cls.tenant, "Principal", "principal", PRINCIPAL_KEYS)
        cls.bursar_role = make_key_role(cls.tenant, "Bursar", "bursar", BURSAR_KEYS)
        cls.viewer_hold_role = make_key_role(
            cls.tenant, "Hold Viewer", "hold_viewer", (PERM_HOLD_VIEW,),
        )
        cls.principal = cls.make_user("principal@brightfield.test", "Pauline", "Principal", cls.principal_role)
        cls.lekki_principal = cls.make_user(
            "lekki.principal@brightfield.test", "Lola", "Lekki", cls.principal_role, branch=cls.lekki,
        )
        cls.ikeja_principal = cls.make_user(
            "ikeja.principal@brightfield.test", "Ike", "Ikeja", cls.principal_role, branch=cls.ikeja,
        )
        cls.bursar = cls.make_user("bursar@brightfield.test", "Bimpe", "Bursar", cls.bursar_role)
        cls.lekki_bursar = cls.make_user(
            "lekki.bursar@brightfield.test", "Bisi", "Lekki", cls.bursar_role, branch=cls.lekki,
        )
        cls.ikeja_bursar = cls.make_user(
            "ikeja.bursar@brightfield.test", "Biyi", "Ikeja", cls.bursar_role, branch=cls.ikeja,
        )
        cls.hold_viewer = cls.make_user(
            "viewer.hold@brightfield.test", "Hana", "Holdview", cls.viewer_hold_role,
        )

        solo_principal_role = make_key_role(cls.solo, "Principal", "principal", PRINCIPAL_KEYS)
        solo_bursar_role = make_key_role(cls.solo, "Bursar", "bursar", BURSAR_KEYS)
        cls.solo_principal = cls.make_user(
            "principal@sunrise.test", "Sola", "Principal", solo_principal_role, tenant=cls.solo,
        )
        cls.solo_bursar = cls.make_user(
            "bursar@sunrise.test", "Segun", "Bursar", solo_bursar_role, tenant=cls.solo,
        )
        set_setting(CFG_RANK_IN_CLASS, False, cls.solo, cls.solo_admin)
        set_setting(CFG_CONDUCT_RATINGS, False, cls.solo, cls.solo_admin)

        cls.nwosu_mother = cls.make_guardian(cls.chiamaka, "Mrs. Ngozi Nwosu", email="ngozi.nwosu@example.ng")
        cls.make_guardian(cls.tunde, "Mrs. Aduke Bello", email="")
        cls.eze_mother_user = make_school_admin(
            None, email="eze.mother@example.ng", tenant=cls.tenant,
            first_name="Ada", last_name="Eze",
        )
        cls.eze_mother = cls.make_guardian(
            cls.kelechi, "Mrs. Ada Eze", email="eze.mother@example.ng", user=cls.eze_mother_user,
        )
        StudentGuardian.all_objects.create(
            tenant=cls.tenant, student=cls.amina, guardian=cls.eze_mother,
            relationship=Relationship.MOTHER, is_primary=True,
        )
        cls.make_guardian(cls.solo_pupil, "Mr. Okoro", email="okoro@example.ng", tenant=cls.solo)
        cls.make_guardian(cls.solo_pupil_two, "Mrs. Ojo", email="ojo@example.ng", tenant=cls.solo)

    # ── builders ──────────────────────────────────────────────────────────

    @classmethod
    def make_guardian(cls, student, name, *, email, user=None, tenant=None):
        guardian = Guardian.all_objects.create(
            tenant=tenant or cls.tenant, full_name=name, email=email,
            phone="08035550101", user=user,
        )
        StudentGuardian.all_objects.create(
            tenant=tenant or cls.tenant, student=student, guardian=guardian,
            relationship=Relationship.MOTHER, is_primary=True,
        )
        return guardian

    @classmethod
    def default_maths(cls):
        return {
            cls.chiamaka: (18, 15, 46), cls.tunde: (15, 15, 45),
            cls.kelechi: (10, 10, 20), cls.amina: (15, 15, 30),
        }

    @classmethod
    def default_french(cls):
        return {cls.chiamaka: (10, 10, 30), cls.tunde: (15, 15, 45)}

    @classmethod
    def tied_maths(cls):
        """Tunde 90, Chiamaka and Amina 70 and Kelechi 40: with the tied French marks, 1st, 2nd, 2nd, 4th."""
        return {
            cls.tunde: (18, 18, 54), cls.chiamaka: (14, 14, 42),
            cls.amina: (14, 14, 42), cls.kelechi: (8, 8, 24),
        }

    @classmethod
    def tied_french(cls):
        return {cls.chiamaka: (14, 14, 42), cls.tunde: (18, 18, 54)}

    @classmethod
    def sheets_in(cls, school_class=None, *, maths=None, french=None):
        """JSS1 A's two sheets, submitted, as the teachers would have left them."""
        school_class = school_class or cls.jss1a
        cls.make_sheet(
            school_class, cls.maths, status=SheetStatus.SUBMITTED,
            rows=maths or cls.default_maths(),
        )
        cls.make_sheet(
            school_class, cls.french, status=SheetStatus.SUBMITTED,
            rows=french or cls.default_french(),
        )

    @classmethod
    def comment_all(cls, user=None, school_class=None, students=None, term=None):
        """The class teacher's comment for every student, through the API."""
        user = user or cls.eze
        school_class = school_class or cls.jss1a
        students = students or (cls.chiamaka, cls.tunde, cls.kelechi, cls.amina)
        client = TenantAPIClient(user)
        with school_clock():
            for student in students:
                response = client.put(
                    reverse(
                        "gradebook-compile",
                        kwargs={"class_id": school_class.pk, "student_id": student.pk},
                    ) + f"?term={(term or cls.first_term).pk}",
                    {"teacher_comment": SENT_COMMENT}, format="json",
                )
                assert response.status_code == 200, response.data

    @classmethod
    def drive_to_awaiting(cls, *, maths=None, french=None):
        """JSS1 A: both sheets in, every comment written, the class submitted to the principal."""
        cls.sheets_in(maths=maths, french=french)
        cls.comment_all()
        with school_clock():
            response = TenantAPIClient(cls.eze).post(
                reverse("gradebook-class-submit", kwargs={"class_id": cls.jss1a.pk})
                + f"?term={cls.first_term.pk}", {}, format="json",
            )
        assert response.status_code == 200, response.data

    # ── the finance reader ────────────────────────────────────────────────

    def finance(self, *owing):
        """Install a fake finance reader. *owing* is ``(student, kobo, oldest unpaid day)``."""
        rows = tuple(
            DebtorRow(
                student_ref=str(student.pk), student_name=student.full_name,
                class_label="", outstanding=kobo, ageing=AgeingBucket.DAYS_31_60,
                oldest_unpaid_on=oldest,
            )
            for student, kobo, oldest in owing
        )
        registry.set_finance_reader(FakeFinanceReader(debtors=rows))
        self.addCleanup(registry.reset)

    def finance_down(self):
        registry.set_finance_reader(unavailable_finance_reader())
        self.addCleanup(registry.reset)

    # ── HTTP ──────────────────────────────────────────────────────────────

    def broadsheet(self, user, school_class=None, **kwargs):
        return self.call(
            "get", user, "gradebook-broadsheet", class_id=(school_class or self.jss1a).pk,
            term=kwargs.pop("term", self.first_term), **kwargs,
        )

    def compile_get(self, user, student, school_class=None, **kwargs):
        return self.call(
            "get", user, "gradebook-compile", class_id=(school_class or self.jss1a).pk,
            student_id=student.pk, term=kwargs.pop("term", self.first_term), **kwargs,
        )

    def compile_put(self, user, student, body, school_class=None, **kwargs):
        return self.call(
            "put", user, "gradebook-compile", body, class_id=(school_class or self.jss1a).pk,
            student_id=student.pk, term=kwargs.pop("term", self.first_term), **kwargs,
        )

    def submit_class(self, user, school_class=None, **kwargs):
        return self.call(
            "post", user, "gradebook-class-submit", class_id=(school_class or self.jss1a).pk,
            term=kwargs.pop("term", self.first_term), **kwargs,
        )

    def approvals(self, user, **query):
        from urllib.parse import urlencode

        url = reverse("gradebook-approvals")
        query.setdefault("term", self.first_term.pk)
        return TenantAPIClient(user).get(f"{url}?{urlencode(query)}")

    def return_class(self, user, note="Please recheck the comments.", school_class=None, **body):
        return self.call(
            "post", user, "gradebook-class-return", {"note": note, **body},
            class_id=(school_class or self.jss1a).pk, term=self.first_term,
        )

    def principal_comments(self, user, school_class=None, **kwargs):
        return self.call(
            "get", user, "gradebook-principal-comments",
            class_id=(school_class or self.jss1a).pk, term=kwargs.pop("term", self.first_term),
            **kwargs,
        )

    def write_principal_comments(self, user, comments, school_class=None):
        return self.call(
            "put", user, "gradebook-principal-comments", {"comments": comments},
            class_id=(school_class or self.jss1a).pk, term=self.first_term,
        )

    def preview(self, user, school_class=None, **kwargs):
        return self.call(
            "get", user, "gradebook-publish-preview", class_id=(school_class or self.jss1a).pk,
            term=kwargs.pop("term", self.first_term), **kwargs,
        )

    def publish(self, user, classes=None, **kwargs):
        """Publish *classes* (default JSS1 A), running the emails the commit would send."""
        ids = [c.pk for c in (classes or [self.jss1a])]
        with self.captureOnCommitCallbacks(execute=True):
            return self.call(
                "post", user, "gradebook-publish", {"classes": ids},
                term=kwargs.pop("term", self.first_term), **kwargs,
            )

    def holds(self, user, **query):
        from urllib.parse import urlencode

        query.setdefault("term", self.first_term.pk)
        return TenantAPIClient(user).get(f"{reverse('gradebook-holds')}?{urlencode(query)}")

    def hold(self, user, student, reason="Fees outstanding since September.", term=None, **body):
        return TenantAPIClient(user).post(
            reverse("gradebook-holds"),
            {"student": student.pk, "term": (term or self.first_term).pk, "reason": reason, **body},
            format="json",
        )

    def lift(self, user, hold_id, **body):
        with self.captureOnCommitCallbacks(execute=True):
            return TenantAPIClient(user).post(
                reverse("gradebook-hold-lift", kwargs={"hold_id": hold_id}), body, format="json",
            )

    @staticmethod
    def row_of(body, student):
        return next(r for r in body["rows"] if r["student"]["id"] == student.pk)

    @staticmethod
    def days_before(day: dt.date, n: int) -> dt.date:
        return day - dt.timedelta(days=n)
