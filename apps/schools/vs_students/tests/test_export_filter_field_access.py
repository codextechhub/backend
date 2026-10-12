"""The students export answers a filter only about what its reader may read.

Brightfield (two branches) and Sunrise (one) each have a teacher who may export
the roll but may not read a pupil's last name or enrolment date. Typing
"Nwosu" into the export's search must not tell her, by its row count, that a
pupil called Nwosu exists; a first name she may read still finds the pupil; and
a filter on the enrolment date she cannot see is refused rather than dropped.
"""
from __future__ import annotations

from vs_exports import engine
from vs_exports.catalogue import ScopeContext, get_dataset
from vs_exports.constants import ExportPermission
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .base import ALL_KEYS, StudentsFixture

LAST = "school.students.last_name"
ENROLLED = "school.students.enrolment_date"


class StudentsExportFilterAccessTests(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.students")
        export_keys = [
            make_permission(key, scope=PermissionScope.TENANT)
            for key in (ExportPermission.CATALOGUE_VIEW, ExportPermission.SENSITIVE_EXPORT)
        ]
        cls.teachers = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            role = make_role(school, name=f"Teacher {label}", key=f"teacher_{label}")
            for perm in ALL_KEYS:
                make_role_permission(role, cls.permissions[perm])
            for perm in export_keys:
                make_role_permission(role, perm)
            set_field_access(role, LAST, ENROLLED, read=False, write=False)
            user = make_school_admin(
                None, email=f"teacher_{label}@exports.test", tenant=school.tenant,
            )
            make_assignment(school, user, role, branch=None)
            cls.teachers[label] = user
        for perm in export_keys:
            make_role_permission(cls.role, perm)
        cls.dataset = get_dataset("school.students")
        # The fixture's data helpers read only class attributes.
        StudentsFixture.student(cls, first="Chiamaka", last="Nwosu")
        StudentsFixture.student(
            cls, first="Chiamaka", last="Nwosu",
            tenant=cls.solo.tenant, branch=cls.solo_branch,
        )

    def matching(self, user, *filters):
        scope = ScopeContext(tenant=user.tenant, user=user)
        return engine.estimate(
            user, self.dataset, scope, {"columns": ["id"], "filters": list(filters)},
            user.tenant,
        )["matching_rows"]

    def test_a_hidden_last_name_matches_nobody(self):
        for label, teacher in self.teachers.items():
            with self.subTest(school=label):
                self.assertEqual(self.matching(teacher, {"id": "search", "value": "Nwosu"}), 0)

    def test_a_first_name_she_may_read_still_finds_the_pupil(self):
        for label, teacher in self.teachers.items():
            with self.subTest(school=label):
                self.assertEqual(
                    self.matching(teacher, {"id": "search", "value": "Chiamaka"}), 1,
                )

    def test_a_default_reader_still_finds_the_pupil_by_last_name(self):
        self.assertEqual(self.matching(self.admin, {"id": "search", "value": "Nwosu"}), 1)

    def test_a_filter_on_the_hidden_enrolment_date_is_refused_with_403(self):
        teacher = self.teachers["multi"]
        response = self.client_for(teacher).post(
            f"/v1/exports/preview/?tenant={self.tenant.slug}",
            {
                "dataset_key": "school.students", "columns": ["id"],
                "filters": [{"id": "enrolment_date", "start": "2026-01-01",
                             "end": "2026-12-31"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_read_denied")

    def test_the_file_header_does_not_echo_the_hidden_filter(self):
        teacher = self.teachers["multi"]
        scope = ScopeContext(tenant=teacher.tenant, user=teacher)
        summary = engine.filters_summary(self.dataset, {"filters": [
            {"id": "search", "value": "Nwosu"},
            {"id": "enrolment_date", "start": "2026-01-01", "end": "2026-12-31"},
        ]}, scope)
        filters = [value for label, value in summary if label == "Filter"]
        self.assertEqual(filters, ["First name, Admission no. mentions “Nwosu”"])

    def test_the_hidden_enrolment_date_column_is_not_written_for_her(self):
        fields, _ = engine.resolve_columns(
            self.teachers["multi"], self.dataset, ["id", "status", "enrolment_date"],
            self.tenant,
        )
        self.assertEqual([f.id for f in fields], ["id", "status"])

    def test_a_hidden_last_name_column_is_not_written_despite_the_sensitive_key(self):
        """The sensitive-export key lets names leave in a file; the switch still decides whose."""
        for label, teacher in self.teachers.items():
            with self.subTest(school=label):
                fields, _ = engine.resolve_columns(
                    teacher, self.dataset, ["id", "first_name", "last_name"],
                    teacher.tenant,
                )
                self.assertEqual([f.id for f in fields], ["id", "first_name"])

    def test_a_default_reader_with_the_sensitive_key_still_gets_last_names(self):
        fields, _ = engine.resolve_columns(
            self.admin, self.dataset, ["id", "first_name", "last_name"], self.tenant,
        )
        self.assertEqual([f.id for f in fields], ["id", "first_name", "last_name"])
