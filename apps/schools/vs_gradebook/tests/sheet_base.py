"""Brightfield and Sunrise with the setup a score sheet needs.

Extends :class:`~.base.GradebookFixture` with what a sheet is made of: a
structure and a scale in force for each programme, the students taking French in
JSS1 A, and builders that write a sheet and its scores straight to the tables so
a test starts from a known state. The sheet endpoints themselves are exercised
through the API.

**What the fixture holds**

* JSS1 A (Lekki, class teacher Mr Eze) has Mathematics, led by Mr Eze with Ms
  Bello assisting, and French, an optional subject taken by Chiamaka and Tunde
  only. Its roll today is Tunde, Kelechi, Chiamaka and Amina (suspended), in
  name order; Obinna joins in November.
* JSS1 B (Lekki) has Mathematics with nobody assigned and French taken by
  nobody. JSS1 C is at Ikeja.
* SSS1 A (Lekki) has Mathematics, led by Mr Adewale, with two students. Sciences
  has no head until a test names one; moderation is off until a test turns it on
  for the ``SSS`` programme (:meth:`moderate_seniors`).
* The structure is two continuous assessments of 20 and an examination of 60, in
  force for both programmes at both schools. The junior scale is A from 70; the
  senior one is the A1 to F9 scale.
* Sunrise's Basic 3 has Mathematics led by Bola Ade, two students and no
  moderation.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import ElectiveTaker

from ..constants import SheetStatus
from ..models import ClassTerm, Score, ScoreSheet
from .base import SENIOR_BANDS, GradebookFixture, set_setting


class SheetFixture(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.junior_structure = cls.make_structure(cls.junior)
        cls.junior_scale = cls.make_scale(cls.junior)
        cls.senior_structure = cls.make_structure(cls.senior)
        cls.senior_scale = cls.make_scale(cls.senior, bands=SENIOR_BANDS)
        cls.solo_structure = cls.make_structure(cls.solo_program, tenant=cls.solo)
        cls.solo_scale = cls.make_scale(cls.solo_program, tenant=cls.solo)
        cls.ca1, cls.ca2, cls.exam = list(cls.junior_structure.components.all())
        cls.sca1, cls.sca2, cls.sexam = list(cls.senior_structure.components.all())
        for pupil in (cls.chiamaka, cls.tunde):
            ElectiveTaker.objects.create(
                tenant=cls.tenant, school_class=cls.jss1a, term=cls.first_term,
                subject=cls.french, student=pupil,
            )

    # ── builders ──────────────────────────────────────────────────────────

    @classmethod
    def moderate_seniors(cls, head=True):
        """Turn moderation on for SSS and, unless told otherwise, make Mr Okeke head Sciences."""
        set_setting("gradebook.moderated_programs", ["SSS"], cls.tenant, cls.admin)
        if head:
            cls.set_head(cls.sciences, cls.okeke_staff)

    @classmethod
    def make_sheet(
        cls, school_class, subject, *, rows=None, status=SheetStatus.IN_PROGRESS,
        term=None, structure=None, needs_moderation=False, branch=None, tenant=None,
        **fields,
    ):
        """A sheet in the given state, with *rows* ``{student: (cell, cell, cell)}`` written to it.

        A cell is a mark, or ``"ABS"`` or ``"EXC"``, or None for a blank.
        """
        tenant = tenant or school_class.tenant
        term = term or (cls.first_term if tenant == cls.tenant else cls.solo_term)
        structure = structure or (
            cls.solo_structure if tenant == cls.solo
            else cls.junior_structure if school_class.level.program_id == cls.junior.pk
            else cls.senior_structure
        )
        class_term = ClassTerm.all_objects.filter(school_class=school_class, term=term).first()
        if class_term is None:
            class_term = cls.make_class_term(
                school_class, term, structure=structure,
                branch=branch or (cls.solo_branch if tenant == cls.solo else None),
            )
        elif class_term.structure_id is None:
            class_term.structure = structure
            class_term.save(update_fields=["structure"])
        sheet = ScoreSheet.all_objects.create(
            tenant=tenant, class_term=class_term, subject=subject, status=status,
            needs_moderation=needs_moderation, **fields,
        )
        components = list(structure.components.all())
        for student, cells in (rows or {}).items():
            for component, cell in zip(components, cells):
                if cell is None:
                    continue
                mark, marker = (None, cell) if isinstance(cell, str) else (cell, None)
                Score.all_objects.create(
                    tenant=tenant, sheet=sheet, student=student, component=component,
                    mark=mark, marker=marker,
                )
        return sheet

    # ── HTTP ──────────────────────────────────────────────────────────────

    def call(self, verb, user, name, body=None, *, term=None, **kwargs):
        """A request to a sheet route, with ``?term=`` when a term is named."""
        url = reverse(name, kwargs=kwargs)
        if term is not None:
            url += f"?term={getattr(term, 'pk', term)}"
        client = TenantAPIClient(user)
        if verb == "get":
            return client.get(url)
        return getattr(client, verb)(url, body or {}, format="json")

    def sheet(self, user, school_class, subject, **kwargs):
        return self.call(
            "get", user, "gradebook-sheet", class_id=school_class.pk, subject_id=subject.pk,
            **kwargs,
        )

    def save(self, user, school_class, subject, cells, **kwargs):
        return self.call(
            "put", user, "gradebook-sheet-scores", {"cells": cells},
            class_id=school_class.pk, subject_id=subject.pk, **kwargs,
        )

    def submit(self, user, school_class, subject, **kwargs):
        return self.call(
            "post", user, "gradebook-sheet-submit",
            class_id=school_class.pk, subject_id=subject.pk, **kwargs,
        )

    def approve(self, user, school_class, subject, **kwargs):
        return self.call(
            "post", user, "gradebook-sheet-approve",
            class_id=school_class.pk, subject_id=subject.pk, **kwargs,
        )

    def send_back(self, user, school_class, subject, note="Recheck the examination.", **kwargs):
        return self.call(
            "post", user, "gradebook-sheet-return", {"note": note},
            class_id=school_class.pk, subject_id=subject.pk, **kwargs,
        )

    def remind(self, user, school_class, subject, **kwargs):
        return self.call(
            "post", user, "gradebook-sheet-remind",
            class_id=school_class.pk, subject_id=subject.pk, **kwargs,
        )

    @staticmethod
    def cell(student, component, mark=None, marker=None):
        """One cell of a batch save."""
        return {"student": student.pk, "component": component.pk, "mark": mark, "marker": marker}

    def full_row(self, student, a=15, b=15, c=45):
        """Three cells that fill a junior student's row."""
        return [
            self.cell(student, self.ca1, a),
            self.cell(student, self.ca2, b),
            self.cell(student, self.exam, c),
        ]

    def senior_row(self, student, a=15, b=15, c=45):
        """Three cells that fill a senior student's row."""
        return [
            self.cell(student, self.sca1, a),
            self.cell(student, self.sca2, b),
            self.cell(student, self.sexam, c),
        ]

    @staticmethod
    def row_for(body, student):
        """The row of *student* in a sheet read."""
        return next(r for r in body["rows"] if r["student"]["id"] == student.pk)
