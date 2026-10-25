"""No academic field reaches a caller who may read none, at any depth.

Every declared surface of ``academics.department``, ``academics.electives`` and
``academics.classes`` is rendered on real records with every field filled in (a
department with a head, a class with recorded takers, a class whose teacher has
changed hands) as a caller whose role has every field of that resource switched
off, and the whole payload is walked.
"""
from __future__ import annotations

from schools.vs_staff.services import teaching
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from ..models import ElectiveTaker
from ..services import electives
from .electives_base import TODAY, ElectivesFixture

_SURFACE = "schools.vs_academics.serializers."


class AcademicsDeepPayloadTests(DeepPayloadChecks, ElectivesFixture):
    resources = frozenset({"academics.department", "academics.electives", "academics.classes"})
    covers = frozenset({
        _SURFACE + "DepartmentSerializer",
        _SURFACE + "DepartmentWriteSerializer",
        _SURFACE + "ElectiveClassTermSerializer",
        _SURFACE + "ElectiveSubjectSerializer",
        _SURFACE + "ElectiveStudentSerializer",
        _SURFACE + "ElectiveChangeSerializer",
        _SURFACE + "SchoolClassDetailSerializer",
        _SURFACE + "ClassTeacherTermSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sciences.head = cls.eze_staff
        cls.sciences.save(update_fields=["head"])
        # Eze holds JSS1 A from before terms were kept; Bello takes it over.
        teaching.set_class_teacher(cls.jss1a, cls.bello_staff, actor=cls.admin, tenant=cls.tenant)
        for student in (cls.tunde, cls.kelechi):
            ElectiveTaker.all_objects.create(
                tenant=cls.tenant, school_class=cls.jss1a, term=cls.first_term,
                subject=cls.yoruba, student=student,
            )

    def samples(self):
        payload = electives.class_term_payload(self.tenant, self.jss1a, self.first_term, TODAY)
        payload["can_edit"] = True
        terms = list(teaching.class_teacher_terms(self.jss1a))
        return [
            Sample(_SURFACE + "SchoolClassDetailSerializer", self.jss1a),
            Sample(_SURFACE + "ClassTeacherTermSerializer", terms, many=True),
            Sample(_SURFACE + "DepartmentSerializer", self.sciences),
            Sample(_SURFACE + "DepartmentWriteSerializer", self.sciences),
            Sample(_SURFACE + "ElectiveClassTermSerializer", payload),
            Sample(_SURFACE + "ElectiveSubjectSerializer", payload["subjects"], many=True),
            Sample(_SURFACE + "ElectiveStudentSerializer", payload["students"], many=True),
            Sample(
                _SURFACE + "ElectiveChangeSerializer",
                [{"student": self.tunde.pk, "subject": self.yoruba.pk, "takes": True}],
                many=True,
            ),
        ]
