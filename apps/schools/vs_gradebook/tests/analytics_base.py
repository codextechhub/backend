"""Brightfield and Sunrise with published results written straight to the tables.

Analytics reads snapshots and nothing else, so this fixture writes the
snapshots a publication would have left (:func:`snapshot`) instead of driving
three classes through entering, compiling and publishing. One test class
(:mod:`.test_analytics_published`) reads what the real publishing and amendment
services leave, so the shape these fixtures stand in for is held to the real one.

**First Term 2025/2026, Mathematics and French, the junior scale** (A from 70,
B from 60, C from 50, D from 40, F below):

========  ===========  =====  ======  =======
Class      Student      Maths  French  Overall
========  ===========  =====  ======  =======
JSS1 A     Chiamaka     80     60      70.00
(Lekki)    Tunde        70     70      70.00
           Kelechi      30     --      30.00
           Amina        60     --      60.00  (held)
JSS1 B     Bisi         50     --      50.00
(Lekki)    Uche         45     --      45.00
JSS1 C     Sade         90     --      90.00
(Ikeja)    Ife          70     --      70.00
========  ===========  =====  ======  =======

So Mathematics averages 60.00 in JSS1 A (75% pass), 47.50 in JSS1 B and 80.00 in
JSS1 C, a gap of 32.50 across the three and 12.50 across the two a Lekki reader
sees. French is JSS1 A's alone. Chiamaka and Tunde tie for top of JSS1 A.

**Earlier terms, for the movers.** 2024/2025 has a Second and a Third Term, each
published in a class of that year. Tunde was 90.00 in the Second and 75.00 in the
Third (so the Third is the one compared with: -5.00), Chiamaka 60.00 in the Third
(+10.00), Sade 99.50 in the Second and has no average in the Third, so the Second
is used (-9.50). Kelechi, Amina and the others have no earlier result. Tunde also
has a result in Second Term 2025/2026, which starts later and is never compared
with.

JSS1 A, JSS1 B and JSS1 C are published for First Term. SSS1 A is awaiting the
principal with snapshots on its results, which analytics must still not read.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import AcademicSession, AcademicTerm, Level, SchoolClass
from schools.vs_gradebook.constants import (
    PERM_ANALYTICS_EXPORT,
    PERM_ANALYTICS_VIEW,
    StoredStage,
)
from schools.vs_gradebook.models import ClassTerm, StudentTermResult

from ..services import compute
from .base import JUNIOR_BANDS, GradebookFixture, make_key_role

def two(value) -> str:
    """*value* as the two-place figure a snapshot carries."""
    return str(compute.round2(Decimal(str(value))))


def snapshot(
    student, school_class, term, lines, *, average, position=None, version=1,
    teacher="Zephyr Quill", bands=JUNIOR_BANDS,
):
    """A published snapshot with the keys analytics reads and the names it must never print.

    *lines* are ``(subject, percentage)`` pairs; the grade and whether it is a
    fail come from *bands*. ``position`` is left out when None, as a school that
    does not rank publishes.
    """
    ordered = sorted(bands, key=lambda band: band[0], reverse=True)
    key, ceiling = [], 100
    for low, grade, remark, is_fail in ordered:
        key.append({
            "min_score": low, "max_score": ceiling, "grade": grade, "remark": remark,
            "is_fail": is_fail,
        })
        ceiling = low - 1
    rows = []
    for subject, percentage in lines:
        band = next(b for b in key if percentage >= b["min_score"]) if percentage is not None else None
        rows.append({
            "subject": {"id": subject.pk, "name": subject.name},
            "percentage": two(percentage) if percentage is not None else None,
            "grade": band["grade"] if band else None,
            "is_fail": band["is_fail"] if band else None,
            "total": int(percentage) if percentage is not None else None,
            "obtainable": 100,
        })
    out = {
        "version": version,
        "student": {
            "id": student.pk, "name": student.full_name, "admission_number": student.student_number,
        },
        "class": {"id": school_class.pk, "name": school_class.name},
        "term": {"id": term.pk, "name": term.name},
        "subjects": rows,
        "overall_average": two(average) if average is not None else None,
        "grading_key": key,
        "class_teacher": teacher,
        "principal": teacher,
    }
    if position is not None:
        out["position"] = position
    return out


class AnalyticsFixture(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.view_role = make_key_role(
            cls.tenant, "Analytics Viewer", "analytics_viewer", (PERM_ANALYTICS_VIEW,),
        )
        cls.export_role = make_key_role(
            cls.tenant, "Analytics Exporter", "analytics_exporter", (PERM_ANALYTICS_EXPORT,),
        )
        cls.both_role = make_key_role(
            cls.tenant, "Analytics Reader", "analytics_reader",
            (PERM_ANALYTICS_VIEW, PERM_ANALYTICS_EXPORT),
        )
        cls.viewer_only = cls.make_user("view@brightfield.test", "Vik", "Viewing", cls.view_role)
        cls.exporter_only = cls.make_user("export@brightfield.test", "Exa", "Export", cls.export_role)
        cls.reader = cls.make_user("reader@brightfield.test", "Rhoda", "Reader", cls.both_role)
        cls.lekki_reader = cls.make_user(
            "reader@lekki.test", "Lola", "Reader", cls.both_role, branch=cls.lekki,
        )
        cls.ikeja_reader = cls.make_user(
            "reader@ikeja.test", "Ike", "Reader", cls.both_role, branch=cls.ikeja,
        )
        cls.solo_reader = cls.make_user(
            "reader@sunrise.test", "Sol", "Reader",
            make_key_role(
                cls.solo, "Analytics Reader", "analytics_reader",
                (PERM_ANALYTICS_VIEW, PERM_ANALYTICS_EXPORT),
            ),
            tenant=cls.solo,
        )

        cls.bisi = cls.make_student("Bisi", "Ade", cls.jss1b, "BFS/010")
        cls.uche = cls.make_student("Uche", "Obi", cls.jss1b, "BFS/011")

        # The year before: two terms, each published in a class of that year.
        cls.old_year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2024/2025",
            start_date=dt.date(2024, 9, 9), end_date=dt.date(2025, 7, 18), status="ARCHIVED",
        )
        cls.old_second = AcademicTerm.all_objects.create(
            tenant=cls.tenant, session=cls.old_year, name="Second Term", order_index=2,
            start_date=dt.date(2025, 1, 13), end_date=dt.date(2025, 4, 4),
        )
        cls.old_third = AcademicTerm.all_objects.create(
            tenant=cls.tenant, session=cls.old_year, name="Third Term", order_index=3,
            start_date=dt.date(2025, 4, 28), end_date=dt.date(2025, 7, 18),
        )
        old_level = Level.all_objects.create(
            tenant=cls.tenant, session=cls.old_year, program=cls.junior,
            name="Primary 6", code="PRI6", order_index=1,
        )
        cls.old_lekki = SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.old_year, level=old_level, name="Primary 6 A",
            code="PRI6A", branch=cls.lekki,
        )
        cls.old_ikeja = SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.old_year, level=old_level, name="Primary 6 C",
            code="PRI6C", branch=cls.ikeja,
        )

        cls.publish_first_term()

    # ── building the published state ──────────────────────────────────────

    @classmethod
    def put(cls, school_class, term, rows, *, stage=StoredStage.PUBLISHED, branch=None):
        """A class-term at *stage* with a result for each ``(student, snapshot)`` of *rows*."""
        tenant = school_class.tenant
        class_term = ClassTerm.all_objects.create(
            tenant=tenant, school_class=school_class, term=term,
            branch=branch or school_class.branch or cls.lekki, stage=stage,
            published_at=timezone.now() if stage == StoredStage.PUBLISHED else None,
        )
        for student, body in rows:
            StudentTermResult.all_objects.create(
                tenant=tenant, class_term=class_term, student=student, snapshot=body,
                version=body["version"] if body else 0,
            )
        return class_term

    @classmethod
    def publish_first_term(cls):
        maths, french, term = cls.maths, cls.french, cls.first_term
        a, b, c = cls.jss1a, cls.jss1b, cls.jss1c
        cls.put(a, term, [
            (cls.chiamaka, snapshot(cls.chiamaka, a, term, [(maths, 80), (french, 60)], average=70, position=1)),
            (cls.tunde, snapshot(cls.tunde, a, term, [(maths, 70), (french, 70)], average=70, position=1)),
            (cls.kelechi, snapshot(cls.kelechi, a, term, [(maths, 30)], average=30, position=4)),
            (cls.amina, snapshot(cls.amina, a, term, [(maths, 60)], average=60, position=3)),
        ])
        cls.put(b, term, [
            (cls.bisi, snapshot(cls.bisi, b, term, [(maths, 50)], average=50, position=1)),
            (cls.uche, snapshot(cls.uche, b, term, [(maths, 45)], average=45, position=2)),
        ])
        cls.put(c, term, [
            (cls.sade, snapshot(cls.sade, c, term, [(maths, 90)], average=90, position=1)),
            (cls.ife, snapshot(cls.ife, c, term, [(maths, 70)], average=70, position=2)),
        ])
        # Awaiting the principal: a snapshot on a result that must never be read.
        sss = cls.sss1a
        cls.put(sss, term, [
            (cls.zainab, snapshot(cls.zainab, sss, term, [(maths, 100)], average=100, position=1)),
            (cls.femi, None),
        ], stage=StoredStage.AWAITING)

        old, other = cls.old_lekki, cls.old_ikeja
        cls.put(old, cls.old_second, [
            (cls.tunde, snapshot(cls.tunde, old, cls.old_second, [(maths, 90)], average=90)),
        ])
        cls.put(old, cls.old_third, [
            (cls.tunde, snapshot(cls.tunde, old, cls.old_third, [(maths, 75)], average=75)),
            (cls.chiamaka, snapshot(cls.chiamaka, old, cls.old_third, [(maths, 60)], average=60)),
        ])
        cls.put(other, cls.old_second, [
            (cls.sade, snapshot(cls.sade, other, cls.old_second, [(maths, 99.5)], average=99.5)),
        ])
        cls.put(other, cls.old_third, [
            (cls.sade, snapshot(cls.sade, other, cls.old_third, [(maths, None)], average=None)),
        ])
        # A later term is never compared with.
        cls.put(a, cls.second_term, [
            (cls.tunde, snapshot(cls.tunde, a, cls.second_term, [(maths, 10)], average=10)),
        ])

    # ── HTTP ──────────────────────────────────────────────────────────────

    def analytics(self, user, **query):
        return TenantAPIClient(user).get(reverse("gradebook-analytics"), query)

    def export(self, user, **query):
        return TenantAPIClient(user).get(reverse("gradebook-analytics-export"), query)

    def subject_row(self, body, subject, school_class):
        return next(
            row for row in body["subjects"]
            if row["subject"]["id"] == subject.pk and row["class"]["id"] == school_class.pk
        )

    @staticmethod
    def texts(payload) -> list:
        """Every string anywhere in *payload*, keys and values."""
        out = []

        def walk(value):
            if isinstance(value, dict):
                for key, inner in value.items():
                    out.append(str(key))
                    walk(inner)
            elif isinstance(value, (list, tuple)):
                for inner in value:
                    walk(inner)
            elif value is not None:
                out.append(str(value))

        walk(payload)
        return out
