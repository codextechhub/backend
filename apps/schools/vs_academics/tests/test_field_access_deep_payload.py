"""No academic field reaches a caller who may read none, at any depth.

Every declared surface of ``academics.department`` and ``academics.electives`` is
rendered on real records with every field filled in (a department with a head, a
class with recorded takers) as a caller whose role has every field of that
resource switched off, and the whole payload is walked.
"""
from __future__ import annotations

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from ..models import ElectiveTaker
from ..services import electives
from .electives_base import TODAY, ElectivesFixture

_SURFACE = "schools.vs_academics.serializers."


class AcademicsDeepPayloadTests(DeepPayloadChecks, ElectivesFixture):
    resources = frozenset({"academics.department", "academics.electives"})
    covers = frozenset({
        _SURFACE + "DepartmentSerializer",
        _SURFACE + "DepartmentWriteSerializer",
        _SURFACE + "ElectiveClassTermSerializer",
        _SURFACE + "ElectiveSubjectSerializer",
        _SURFACE + "ElectiveStudentSerializer",
        _SURFACE + "ElectiveChangeSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sciences.head = cls.eze_staff
        cls.sciences.save(update_fields=["head"])
        for student in (cls.tunde, cls.kelechi):
            ElectiveTaker.all_objects.create(
                tenant=cls.tenant, school_class=cls.jss1a, term=cls.first_term,
                subject=cls.yoruba, student=student,
            )

    def samples(self):
        payload = electives.class_term_payload(self.tenant, self.jss1a, self.first_term, TODAY)
        payload["can_edit"] = True
        return [
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
