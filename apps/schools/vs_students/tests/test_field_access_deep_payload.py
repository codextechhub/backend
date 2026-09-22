"""No pupil or guardian detail reaches a caller who may read none, at any depth.

Every declared surface of ``school.students`` and ``school.guardians`` is
rendered on a real pupil and a real guardian whose registered fields are all
filled in, as a caller whose role has every field of that resource switched
off. The whole payload is walked, so a guardian nested inside a student's
guardian list is held to the same switch as the guardian's own record.

Both shapes of school: Brightfield has two branches and Sunrise has one, where
the branch columns recede.
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Count

from schools.vs_students.constants import Relationship
from schools.vs_students.models import Guardian, Student, StudentGuardian
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from .base import StudentsFixture

_SURFACE = "schools.vs_students.serializers."


class StudentsDeepPayloadTests(DeepPayloadChecks, StudentsFixture):
    resources = frozenset({"school.students", "school.guardians"})
    covers = frozenset({
        _SURFACE + "StudentListSerializer",
        _SURFACE + "StudentDetailSerializer",
        _SURFACE + "StudentWriteSerializer",
        _SURFACE + "EnrolmentWriteSerializer",
        _SURFACE + "GuardianSerializer",
        _SURFACE + "GuardianDirectorySerializer",
        _SURFACE + "GuardianUpdateSerializer",
        _SURFACE + "GuardianWriteSerializer",
        _SURFACE + "GuardianLinkSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.records = []
        for tenant, branch, school_class in (
            (cls.tenant, cls.lekki, cls.shared_class),
            (cls.solo.tenant, cls.solo_branch, None),
        ):
            pupil = Student.all_objects.create(
                tenant=tenant, branch=branch, first_name="Chiamaka",
                last_name="Nwosu", date_of_birth=dt.date(2013, 4, 18),
                gender="FEMALE", status="ACTIVE",
                blood_group="O+", allergies="Peanuts", conditions="Asthma",
                enrolment_date=dt.date(2024, 9, 9),
                phone="08030000001", email="chiamaka@example.ng",
                address="4 Allen Avenue, Ikeja",
            )
            guardian = Guardian.all_objects.create(
                tenant=tenant, full_name="Mr. Chukwudi Nwosu",
                phone="08035550101", email="chukwudi@example.ng",
                address="4 Allen Avenue, Ikeja", occupation="Engineer",
            )
            link = StudentGuardian.all_objects.create(
                tenant=tenant, student=pupil, guardian=guardian,
                relationship=Relationship.FATHER, is_primary=True,
            )
            cls.records.append((tenant, pupil, guardian, link))

    def samples(self):
        samples = []
        for tenant, pupil, guardian, link in self.records:
            multi_branch = tenant.pk == self.tenant.pk
            context = {"multi_branch": multi_branch}
            pupils = Student.all_objects.filter(pk=pupil.pk).prefetch_related(
                "guardian_links__guardian", "enrolments",
            )
            guardian_row = Guardian.all_objects.filter(pk=guardian.pk).annotate(
                ward_count=Count("student_links", distinct=True),
            )
            guardian_values = {
                "guardian_id": None, "full_name": guardian.full_name,
                "phone": guardian.phone, "email": guardian.email,
                "occupation": guardian.occupation, "address": guardian.address,
                "relationship": Relationship.FATHER, "is_primary": True,
            }
            samples += [
                Sample(_SURFACE + "StudentListSerializer", pupils, context,
                       many=True, tenant=tenant),
                Sample(_SURFACE + "StudentDetailSerializer", pupil, context, tenant=tenant),
                Sample(_SURFACE + "StudentWriteSerializer", pupil, context, tenant=tenant),
                # The enrol form is never rendered from a record, so it is given
                # the values an enrolment of this pupil would carry.
                Sample(_SURFACE + "EnrolmentWriteSerializer", {
                    "first_name": pupil.first_name, "middle_name": "",
                    "last_name": pupil.last_name,
                    "date_of_birth": pupil.date_of_birth, "gender": pupil.gender,
                    "nationality": "", "state_of_origin": "",
                    "address": pupil.address, "phone": pupil.phone,
                    "email": pupil.email, "previous_school": "",
                    "blood_group": pupil.blood_group, "allergies": pupil.allergies,
                    "conditions": pupil.conditions,
                    "emergency_contact_name": "", "emergency_contact_phone": "",
                    "student_number": "", "enrolment_date": pupil.enrolment_date,
                    "branch": "", "school_class": None, "applied_for": None,
                    "as_applicant": False, "allow_over_capacity": False,
                    "confirm_duplicate": False, "guardians": [guardian_values],
                }, context, tenant=tenant),
                Sample(_SURFACE + "GuardianSerializer", guardian, context, tenant=tenant),
                Sample(_SURFACE + "GuardianDirectorySerializer", guardian_row,
                       {**context, "wards": {guardian.pk: [pupil.full_name]}},
                       many=True, tenant=tenant),
                Sample(_SURFACE + "GuardianUpdateSerializer", guardian, context,
                       tenant=tenant),
                Sample(_SURFACE + "GuardianWriteSerializer", guardian_values, context,
                       tenant=tenant),
                Sample(_SURFACE + "GuardianLinkSerializer", [link],
                       {**context, "siblings": {}, "class_names": {}},
                       many=True, tenant=tenant),
            ]
        return samples
