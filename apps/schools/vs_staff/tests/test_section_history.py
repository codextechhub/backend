"""A staff section's timeline follows its own read permission."""
from __future__ import annotations

from schools.vs_staff.models import StaffDocument, StaffQualification
from schools.vs_staff.services import visibility
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .base import StaffFixture


class StaffSectionHistoryTests(StaffFixture):
    def history(self, user, staff, section, page=1):
        return self.get(user, "staff-section-history", {
            "section": section, "page": page,
        }, pk=staff.pk)

    def test_qualification_edits_show_before_and_after_without_private_notes(self):
        added = self.post(self.admin, "staff-qualifications", {
            "qualification": "B.Ed Mathematics", "institution": "UNILAG",
            "year_obtained": 2018, "note": "Private office note",
        }, pk=self.eze.pk)
        self.assertEqual(added.status_code, 201, added.data)
        row_id = added.data["data"]["id"]
        updated = self.patch(self.admin, "staff-qualification-detail", {
            "institution": "University of Lagos", "note": "Another private note",
        }, pk=row_id)
        self.assertEqual(updated.status_code, 200, updated.data)

        response = self.history(self.eze.user, self.eze, "qualifications")
        self.assertEqual(response.status_code, 200, response.data)
        entries = response.data["data"]["entries"]
        change = next(row for row in entries if row["action"] == "Changed")
        self.assertEqual(change["changes"], [{
            "field": "Institution", "before": "UNILAG",
            "after": "University of Lagos",
        }])
        self.assertNotIn("private", str(response.data).lower())
        self.assertNotIn("note", str(response.data).lower())
        self.assertIsNone(response.data["data"]["next_page"])

    def test_section_permission_and_tenant_scope(self):
        self.assertEqual(self.history(self.nobody, self.eze, "qualifications").status_code, 403)
        self.assertEqual(self.history(self.solo_admin, self.eze, "qualifications").status_code, 404)
        self.assertEqual(self.history(self.lekki_head, self.ikeja_teacher, "qualifications").status_code, 404)

    def test_self_loses_section_history_when_school_hides_that_section(self):
        policy = {key: list(groups) for key, groups in visibility.DEFAULT_POLICY.items()}
        policy["SELF"].remove("records")
        visibility.write_policy(self.tenant, self.admin, policy)
        self.assertEqual(self.history(self.eze.user, self.eze, "qualifications").status_code, 403)
        self.assertEqual(self.history(self.eze.user, self.eze, "teaching").status_code, 200)

    def test_teaching_and_posting_show_named_before_and_after(self):
        assigned = self.post(self.admin, "staff-teaching", {
            "school_class": self.lekki_class.pk, "subject": self.maths.pk,
            "part": "LEAD",
        }, pk=self.eze.pk)
        self.assertEqual(assigned.status_code, 201, assigned.data)
        changed = self.patch(self.admin, "staff-teaching-detail", {
            "part": "ASSISTANT",
        }, pk=assigned.data["data"]["id"])
        self.assertEqual(changed.status_code, 200, changed.data)
        teaching = self.history(self.eze.user, self.eze, "teaching")
        self.assertEqual(teaching.status_code, 200, teaching.data)
        self.assertEqual(teaching.data["data"]["entries"][0]["changes"], [{
            "field": "Teaching part", "before": "Main teacher", "after": "Assisting",
        }])
        self.assertIn("Mathematics in JSS1 B", teaching.data["data"]["entries"][0]["title"])

        self.eze.branch = self.ikeja
        self.eze.save(update_fields=["branch"])
        posting = self.history(self.eze.user, self.eze, "overview")
        self.assertEqual(posting.status_code, 200, posting.data)
        self.assertIn({
            "field": "Home branch", "before": "Lekki", "after": "Ikeja",
        }, posting.data["data"]["entries"][0]["changes"])

    def test_access_history_names_role_without_grant_notes(self):
        response = self.history(self.eze.user, self.eze, "access")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["entries"][0]["title"], "Teacher")
        self.assertNotIn("reason_note", str(response.data))

    def test_hidden_employment_field_has_no_visible_history(self):
        install_declared_fields("school.teachers")
        set_field_access(self.role, "school.teachers.job_title", read=False, write=False)
        self.eze.job_title = "Head of Mathematics"
        self.eze.save(update_fields=["job_title"])

        detail = self.get(self.admin, "staff-detail", pk=self.eze.pk)
        history = self.history(self.admin, self.eze, "overview")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(history.status_code, 200, history.data)
        self.assertNotIn("job_title", detail.data["data"])
        self.assertNotIn("Head of Mathematics", str(history.data))
        self.assertNotIn("Job title", str(history.data))

    def test_contact_history_stays_available_when_employment_is_hidden(self):
        policy = {key: list(groups) for key, groups in visibility.DEFAULT_POLICY.items()}
        policy["SELF"].remove("employment")
        visibility.write_policy(self.tenant, self.admin, policy)
        self.eze.user.phone = "08021234567"
        self.eze.user.save(update_fields=["phone"])

        history = self.history(self.eze.user, self.eze, "overview")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertIn({
            "field": "Phone", "before": None, "after": "08021234567",
        }, history.data["data"]["entries"][0]["changes"])
        self.assertNotIn("Job title", str(history.data))

    def test_baseline_history_excludes_values_that_look_unchanged(self):
        history = self.history(self.admin, self.eze, "overview")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertNotIn("School-wide", str(history.data))
        for entry in history.data["data"]["entries"]:
            for change in entry["changes"]:
                self.assertNotEqual(change["before"], change["after"])

    def test_replaced_document_shows_change_without_file_paths(self):
        document = StaffDocument.all_objects.create(
            tenant=self.tenant, staff=self.eze, document_type="CV", title="CV",
            file="staff/private/first.pdf",
        )
        document.file = "staff/private/replacement.pdf"
        document.save(update_fields=["file"])

        history = self.history(self.eze.user, self.eze, "documents")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertIn({
            "field": "File", "before": "Previous stored file",
            "after": "Replacement stored file",
        }, history.data["data"]["entries"][0]["changes"])
        self.assertNotIn("staff/private", str(history.data))

    def test_self_history_hides_administrator_reasons(self):
        changed = self.post(self.admin, "staff-status", {
            "to_status": "SUSPENDED", "reason": "Private discipline note",
        }, pk=self.eze.pk)
        self.assertEqual(changed.status_code, 200, changed.data)
        restored = self.post(self.admin, "staff-status", {
            "to_status": "ACTIVE",
        }, pk=self.eze.pk)
        self.assertEqual(restored.status_code, 200, restored.data)
        own = self.get(self.eze.user, "staff-history", pk=self.eze.pk)
        admin = self.get(self.admin, "staff-history", pk=self.eze.pk)
        self.assertEqual(own.status_code, 200, own.data)
        self.assertEqual(admin.status_code, 200, admin.data)
        self.assertNotIn("Private discipline note", str(own.data))
        self.assertIn("Private discipline note", str(admin.data))

    def test_empty_section_is_a_list_and_invalid_section_is_rejected(self):
        response = self.history(self.admin, self.eze, "documents")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], {"entries": [], "next_page": None})
        bad = self.history(self.admin, self.eze, "secrets")
        self.assertEqual(bad.status_code, 400, bad.data)

    def test_sections_are_paginated(self):
        for index in range(14):
            StaffQualification.all_objects.create(
                tenant=self.tenant, staff=self.eze,
                qualification=f"Certificate {index}",
            )
        first = self.history(self.admin, self.eze, "qualifications").data["data"]
        second = self.history(self.admin, self.eze, "qualifications", page=2).data["data"]
        self.assertEqual(len(first["entries"]), 12)
        self.assertEqual(first["next_page"], 2)
        self.assertEqual(len(second["entries"]), 2)
        self.assertIsNone(second["next_page"])
