"""Assessment structures: who may read and write them, the lifecycle and the locks."""
from __future__ import annotations

from vs_audit.models import AuditEvent

from ..constants import SetupStatus
from ..models import (
    AssessmentComponent,
    AssessmentStructure,
    Score,
    ScoreSheet,
)
from .base import STANDARD_COMPONENTS, GradebookFixture


class StructureAccessTests(GradebookFixture):
    """A caller without the key is refused, and a branch-bound caller cannot change shared setup."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.structure = cls.make_structure(cls.junior, status=SetupStatus.DRAFT)

    def test_a_caller_without_the_view_key_is_refused_every_read(self):
        for user in (self.nobody, self.updater, self.eze):
            with self.subTest(user=user.email):
                self.assertEqual(self.get(user, "gradebook-structures").status_code, 403)
                response = self.get(user, "gradebook-structure", pk=self.structure.pk)
                self.assertEqual(response.status_code, 403)

    def test_a_caller_with_only_the_view_key_reads_but_cannot_write(self):
        self.assertEqual(self.get(self.viewer, "gradebook-structures").status_code, 200)
        body = {
            "name": "New", "program": self.junior.pk,
            "components": self.components_body(),
        }
        pk = self.structure.pk
        refusals = [
            self.post(self.viewer, "gradebook-structures", body),
            self.patch(self.viewer, "gradebook-structure", {"name": "Renamed"}, pk=pk),
            self.delete(self.viewer, "gradebook-structure", pk=pk),
            self.post(self.viewer, "gradebook-structure-copy", pk=pk),
            self.post(self.viewer, "gradebook-structure-activate", pk=pk),
        ]
        for response in refusals:
            self.assertEqual(response.status_code, 403)
        self.structure.refresh_from_db()
        self.assertEqual(self.structure.name, "Standard")

    def test_a_branch_bound_caller_holding_the_update_key_is_refused_every_write(self):
        pk = self.structure.pk
        refusals = [
            self.post(self.lekki_admin, "gradebook-structures", {
                "name": "New", "program": self.junior.pk, "components": self.components_body(),
            }),
            self.patch(self.lekki_admin, "gradebook-structure", {"name": "Renamed"}, pk=pk),
            self.delete(self.lekki_admin, "gradebook-structure", pk=pk),
            self.post(self.lekki_admin, "gradebook-structure-copy", pk=pk),
            self.post(self.lekki_admin, "gradebook-structure-activate", pk=pk),
        ]
        for response in refusals:
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.code(response), "SHARED_RECORD_READ_ONLY")
        self.assertEqual(AssessmentStructure.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(self.get(self.lekki_admin, "gradebook-structures").status_code, 200)

    def test_another_schools_structure_is_a_404_and_never_listed(self):
        theirs = self.make_structure(self.solo_program, tenant=self.solo, name="Theirs")
        response = self.get(self.admin, "gradebook-structure", pk=theirs.pk)
        self.assertEqual(response.status_code, 404)
        names = {row["name"] for row in self.data(self.get(self.admin, "gradebook-structures"))}
        self.assertNotIn("Theirs", names)
        for response in (
            self.patch(self.admin, "gradebook-structure", {"name": "Mine now"}, pk=theirs.pk),
            self.delete(self.admin, "gradebook-structure", pk=theirs.pk),
            self.post(self.admin, "gradebook-structure-copy", pk=theirs.pk),
            self.post(self.admin, "gradebook-structure-activate", pk=theirs.pk),
        ):
            self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.name, "Theirs")

    def test_a_program_of_another_school_cannot_be_used(self):
        response = self.post(self.admin, "gradebook-structures", {
            "name": "Sneaky", "program": self.solo_program.pk,
            "components": self.components_body(),
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "UNKNOWN_PROGRAM")
        self.assertFalse(AssessmentStructure.objects.filter(name="Sneaky").exists())

    def test_a_branch_of_another_school_cannot_be_used(self):
        response = self.post(self.admin, "gradebook-structures", {
            "name": "Sneaky", "program": self.junior.pk, "branch": self.solo_branch.pk,
            "components": self.components_body(),
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "UNKNOWN_BRANCH")


class StructureReadTests(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.in_use = cls.make_structure(cls.junior, name="Junior standard")
        cls.draft = cls.make_structure(cls.junior, status=SetupStatus.DRAFT, name="Junior draft")
        cls.senior_structure = cls.make_structure(
            cls.senior, name="Senior standard",
            components=(("Test", "T", 30, False), ("Exam", "EX", 70, True)),
        )
        cls.ikeja_only = cls.make_structure(
            cls.junior, branch=cls.ikeja, status=SetupStatus.DRAFT, name="Ikeja draft",
        )

    def test_a_row_carries_its_components_total_lock_and_scope(self):
        rows = {r["name"]: r for r in self.data(self.get(self.admin, "gradebook-structures"))}
        row = rows["Junior standard"]
        self.assertEqual(row["program"], self.junior.pk)
        self.assertEqual(row["program_name"], "Junior Secondary")
        self.assertIsNone(row["branch"])
        self.assertEqual(row["scope_label"], "School-wide")
        self.assertTrue(row["is_shared"])
        self.assertEqual(row["status"], "IN_USE")
        self.assertEqual(row["total"], 100)
        self.assertEqual(row["total_check"], {"ok": True, "message": "The components total 100."})
        self.assertFalse(row["locked"])
        self.assertIsNone(row["lock_reason"])
        self.assertEqual(
            [(c["name"], c["short_name"], c["max_score"], c["is_exam"], c["order"])
             for c in row["components"]],
            [("1st CA", "CA1", 20, False, 1), ("2nd CA", "CA2", 20, False, 2),
             ("Examination", "EXAM", 60, True, 3)],
        )

    def test_the_branch_of_a_branch_specific_structure_is_named(self):
        rows = {r["name"]: r for r in self.data(self.get(self.admin, "gradebook-structures"))}
        row = rows["Ikeja draft"]
        self.assertEqual(row["branch"], self.ikeja.pk)
        self.assertEqual(row["scope_label"], "Ikeja Branch")
        self.assertFalse(row["is_shared"])

    def test_filters_by_programme_and_status(self):
        def names(**query):
            return {r["name"] for r in self.data(self.get(self.admin, "gradebook-structures", query))}

        self.assertEqual(names(program=self.senior.pk), {"Senior standard"})
        self.assertEqual(names(status="DRAFT"), {"Junior draft", "Ikeja draft"})
        self.assertEqual(
            names(program=self.junior.pk, status="IN_USE"), {"Junior standard"},
        )
        self.assertEqual(names(program=self.senior.pk, status="DRAFT"), set())

    def test_a_branch_bound_reader_sees_shared_records_and_their_own_branch_only(self):
        lekki_sees = {r["name"] for r in self.data(self.get(self.lekki_admin, "gradebook-structures"))}
        self.assertEqual(lekki_sees, {"Junior standard", "Junior draft", "Senior standard"})
        ikeja_sees = {r["name"] for r in self.data(self.get(self.ikeja_admin, "gradebook-structures"))}
        self.assertEqual(ikeja_sees, lekki_sees | {"Ikeja draft"})
        hidden = self.get(self.lekki_admin, "gradebook-structure", pk=self.ikeja_only.pk)
        self.assertEqual(hidden.status_code, 404)
        shown = self.get(self.lekki_admin, "gradebook-structure", pk=self.in_use.pk)
        self.assertEqual(shown.status_code, 200, shown.data)

    def test_a_school_with_one_branch_names_no_branch_at_all(self):
        self.make_structure(self.solo_program, tenant=self.solo, name="Only")
        rows = self.data(self.get(self.solo_admin, "gradebook-structures"))
        self.assertEqual([r["name"] for r in rows], ["Only"])
        self.assertNotIn("scope_label", rows[0])
        self.assertNotIn("is_shared", rows[0])
        self.assertIsNone(rows[0]["branch"])

    def test_an_empty_school_gets_an_empty_list(self):
        response = self.get(self.solo_admin, "gradebook-structures")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response), [])


class StructureCreateTests(GradebookFixture):
    def create(self, **overrides):
        body = {
            "name": "Termly", "program": self.junior.pk,
            "components": self.components_body(),
            **overrides,
        }
        return self.post(self.admin, "gradebook-structures", body)

    def test_creates_a_draft_with_its_components_in_order(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        row = self.data(response)
        self.assertEqual(row["status"], "DRAFT")
        self.assertEqual(row["total"], 100)
        self.assertEqual([c["order"] for c in row["components"]], [1, 2, 3])
        self.assertIsNone(row["copied_from"])
        stored = AssessmentStructure.objects.get(pk=row["id"])
        self.assertEqual(stored.created_by, self.admin)
        self.assertEqual(stored.tenant, self.tenant)

    def test_a_draft_may_be_unsound_because_it_is_checked_only_when_put_in_use(self):
        response = self.create(components=[
            {"name": "A", "short_name": "A", "max_score": 40, "is_exam": True},
            {"name": "B", "short_name": "B", "max_score": 59, "is_exam": True},
        ])
        self.assertEqual(response.status_code, 201, response.data)
        row = self.data(response)
        self.assertEqual(row["total"], 99)
        self.assertFalse(row["total_check"]["ok"])

    def test_a_branch_specific_structure_is_created_for_that_branch(self):
        response = self.create(branch=self.ikeja.pk)
        self.assertEqual(self.data(response)["branch"], self.ikeja.pk)

    def test_a_create_must_name_everything(self):
        for field, message in (
            ("name", "Give it a name."),
            ("program", "Choose the programme it is for."),
            ("components", "Add at least one component."),
        ):
            body = {
                "name": "Termly", "program": self.junior.pk,
                "components": self.components_body(),
            }
            del body[field]
            with self.subTest(field=field):
                response = self.post(self.admin, "gradebook-structures", body)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, str(response.data))

    def test_each_component_is_validated(self):
        base = {"name": "X", "short_name": "X", "max_score": 20, "is_exam": False}
        cases = {
            "zero": ({**base, "max_score": 0}, "at least 1 mark"),
            "over": ({**base, "max_score": 101}, "more than 100"),
            "decimal": ({**base, "max_score": 20.5}, "whole number"),
            "blank name": ({**base, "name": ""}, "may not be blank"),
            "long short name": ({**base, "short_name": "x" * 13}, "12 characters"),
        }
        for label, (row, expected) in cases.items():
            with self.subTest(case=label):
                response = self.create(components=[row])
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, str(response.data))

    def test_two_components_cannot_share_a_name_or_a_short_name(self):
        rows = [
            {"name": "CA", "short_name": "A", "max_score": 40, "is_exam": False},
            {"name": "ca", "short_name": "B", "max_score": 60, "is_exam": True},
        ]
        response = self.create(components=rows)
        self.assertEqual(response.status_code, 400)
        self.assertIn("same name", str(response.data))
        rows[1]["name"] = "Exam"
        rows[1]["short_name"] = "a"
        self.assertEqual(self.create(components=rows).status_code, 400)

    def test_a_structure_cannot_have_more_than_twelve_components(self):
        rows = [
            {"name": f"C{i}", "short_name": f"C{i}", "max_score": 5, "is_exam": False}
            for i in range(13)
        ]
        self.assertEqual(self.create(components=rows).status_code, 400)

    def test_creating_is_audited(self):
        row = self.data(self.create())
        event = AuditEvent.objects.get(entity_type="AssessmentStructure", entity_id=str(row["id"]))
        self.assertEqual(event.module_key, "GRADEBOOK")
        self.assertEqual(event.action_type, "CREATE")
        self.assertEqual(event.tenant, self.tenant)
        self.assertEqual(event.actor_user, self.admin)
        self.assertIn("Junior Secondary, every branch", event.summary)

    def test_at_a_school_with_one_branch_the_same_works(self):
        response = self.post(self.solo_admin, "gradebook-structures", {
            "name": "Basic", "program": self.solo_program.pk,
            "components": self.components_body(),
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.data(response)["status"], "DRAFT")


class StructureEditTests(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.draft = cls.make_structure(cls.junior, status=SetupStatus.DRAFT, name="Draft")
        cls.in_use = cls.make_structure(cls.senior, name="Senior in use")

    def components_of(self, structure):
        return [
            {"id": c.pk, "name": c.name, "short_name": c.short_name,
             "max_score": c.max_score, "is_exam": c.is_exam}
            for c in structure.components.all()
        ]

    def test_a_draft_is_renamed_and_its_components_replaced_keeping_the_ids(self):
        rows = self.components_of(self.draft)
        keep_id = rows[0]["id"]
        rows[0]["max_score"] = 30
        rows[1] = {"name": "Project", "short_name": "PRJ", "max_score": 10, "is_exam": False}
        response = self.patch(
            self.admin, "gradebook-structure",
            {"name": "Renamed", "components": rows}, pk=self.draft.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = self.data(response)
        self.assertEqual(row["name"], "Renamed")
        self.assertEqual(
            [(c["name"], c["max_score"]) for c in row["components"]],
            [("1st CA", 30), ("Project", 10), ("Examination", 60)],
        )
        self.assertEqual(row["components"][0]["id"], keep_id)
        self.assertFalse(AssessmentComponent.objects.filter(name="2nd CA", structure=self.draft).exists())

    def test_a_row_naming_a_component_of_another_structure_is_refused(self):
        theirs = self.in_use.components.first()
        rows = self.components_of(self.draft)
        rows[0]["id"] = theirs.pk
        response = self.patch(
            self.admin, "gradebook-structure", {"components": rows}, pk=self.draft.pk,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "UNKNOWN_ROW")

    def test_the_programme_cannot_be_changed(self):
        response = self.patch(
            self.admin, "gradebook-structure", {"program": self.senior.pk}, pk=self.draft.pk,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be changed", str(response.data))

    def test_a_draft_moves_to_a_branch_and_back_to_every_branch(self):
        response = self.patch(
            self.admin, "gradebook-structure", {"branch": self.ikeja.pk}, pk=self.draft.pk,
        )
        self.assertEqual(self.data(response)["branch"], self.ikeja.pk)
        response = self.patch(
            self.admin, "gradebook-structure", {"branch": None}, pk=self.draft.pk,
        )
        self.assertIsNone(self.data(response)["branch"])

    def test_an_edit_that_changes_nothing_writes_and_audits_nothing(self):
        before = AuditEvent.objects.filter(entity_type="AssessmentStructure").count()
        response = self.patch(
            self.admin, "gradebook-structure",
            {"name": "Draft", "components": self.components_of(self.draft)},
            pk=self.draft.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(AuditEvent.objects.filter(entity_type="AssessmentStructure").count(), before)

    def test_an_edit_is_audited_with_what_changed(self):
        self.patch(self.admin, "gradebook-structure", {"name": "Second draft"}, pk=self.draft.pk)
        event = AuditEvent.objects.filter(
            entity_type="AssessmentStructure", entity_id=str(self.draft.pk), action_type="UPDATE",
        ).latest("event_at")
        self.assertIn("was edited", event.summary)
        self.assertEqual(event.metadata["changes"]["name"], ["Draft", "Second draft"])

    def test_one_in_use_may_be_edited_while_it_stays_sound(self):
        rows = self.components_of(self.in_use)
        rows[0]["max_score"] = 25
        rows[2]["max_score"] = 55
        ok = self.patch(self.admin, "gradebook-structure", {"components": rows}, pk=self.in_use.pk)
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(self.data(ok)["total"], 100)

    def test_one_in_use_cannot_be_left_unsound(self):
        rows = self.components_of(self.in_use)
        rows[0]["max_score"] = 25
        response = self.patch(
            self.admin, "gradebook-structure", {"components": rows}, pk=self.in_use.pk,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "STRUCTURE_TOTAL")
        self.assertEqual(self.in_use.components.first().max_score, 20)

    def test_the_branch_of_one_in_use_cannot_be_moved(self):
        response = self.patch(
            self.admin, "gradebook-structure", {"branch": self.lekki.pk}, pk=self.in_use.pk,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "NOT_A_DRAFT")

    def test_a_retired_structure_cannot_be_changed(self):
        retired = self.make_structure(self.junior, status=SetupStatus.RETIRED, name="Old")
        response = self.patch(self.admin, "gradebook-structure", {"name": "New"}, pk=retired.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "NOT_A_DRAFT")


class StructureDeleteAndCopyTests(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.draft = cls.make_structure(cls.junior, status=SetupStatus.DRAFT, name="Draft")
        cls.in_use = cls.make_structure(cls.senior, name="Senior in use", branch=cls.ikeja)

    def test_a_draft_is_deleted_with_its_components(self):
        pk = self.draft.pk
        response = self.delete(self.admin, "gradebook-structure", pk=pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(AssessmentStructure.objects.filter(pk=pk).exists())
        self.assertFalse(AssessmentComponent.objects.filter(structure_id=pk).exists())
        event = AuditEvent.objects.get(entity_type="AssessmentStructure", entity_id=str(pk), action_type="DELETE")
        self.assertIn("was deleted", event.summary)

    def test_only_a_draft_is_deleted(self):
        for status in (SetupStatus.IN_USE, SetupStatus.RETIRED):
            structure = self.make_structure(self.senior, branch=self.lekki, status=status)
            with self.subTest(status=status):
                response = self.delete(self.admin, "gradebook-structure", pk=structure.pk)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.code(response), "NOT_A_DRAFT")
                self.assertTrue(AssessmentStructure.objects.filter(pk=structure.pk).exists())

    def test_a_copy_is_a_new_draft_that_remembers_its_source(self):
        response = self.post(self.admin, "gradebook-structure-copy", pk=self.in_use.pk)
        self.assertEqual(response.status_code, 201, response.data)
        row = self.data(response)
        self.assertEqual(row["status"], "DRAFT")
        self.assertEqual(row["copied_from"], self.in_use.pk)
        self.assertEqual(row["name"], "Senior in use (copy)")
        self.assertEqual(row["branch"], self.ikeja.pk)
        self.assertEqual(row["program"], self.senior.pk)
        self.assertEqual(
            [(c["name"], c["max_score"], c["is_exam"]) for c in row["components"]],
            [(n, m, e) for n, _, m, e in STANDARD_COMPONENTS],
        )
        source_ids = set(self.in_use.components.values_list("pk", flat=True))
        self.assertFalse(source_ids & {c["id"] for c in row["components"]})
        self.assertEqual(AssessmentStructure.objects.get(pk=self.in_use.pk).status, "IN_USE")

    def test_copying_is_audited(self):
        row = self.data(self.post(self.admin, "gradebook-structure-copy", pk=self.in_use.pk))
        event = AuditEvent.objects.get(entity_type="AssessmentStructure", entity_id=str(row["id"]))
        self.assertIn("was copied from Senior in use", event.summary)
        self.assertEqual(event.metadata["copied_from"], self.in_use.pk)

    def test_a_long_name_is_cut_to_fit_when_copied(self):
        self.in_use.name = "N" * 80
        self.in_use.save()
        row = self.data(self.post(self.admin, "gradebook-structure-copy", pk=self.in_use.pk))
        self.assertEqual(len(row["name"]), 80)


class StructureActivateTests(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.old_school_wide = cls.make_structure(cls.junior, name="Old school-wide")
        cls.old_ikeja = cls.make_structure(cls.junior, branch=cls.ikeja, name="Old Ikeja")
        cls.old_senior = cls.make_structure(cls.senior, name="Old senior")

    def draft(self, **kwargs):
        return self.make_structure(self.junior, status=SetupStatus.DRAFT, name="New", **kwargs)

    def activate(self, structure):
        return self.post(self.admin, "gradebook-structure-activate", pk=structure.pk)

    def status(self, structure):
        return AssessmentStructure.objects.get(pk=structure.pk).status

    def test_a_total_of_ninety_nine_is_refused_with_the_reason(self):
        draft = self.draft(components=(("A", "A", 40, False), ("B", "B", 59, True)))
        response = self.activate(draft)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "STRUCTURE_TOTAL")
        self.assertEqual(
            self.message(response),
            "The components add up to 99, not 100. Adjust the maximums until they total 100.",
        )
        self.assertEqual(self.status(draft), "DRAFT")
        self.assertEqual(self.status(self.old_school_wide), "IN_USE")

    def test_two_examinations_are_refused(self):
        draft = self.draft(components=(("A", "A", 40, True), ("B", "B", 60, True)))
        response = self.activate(draft)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "STRUCTURE_TOTAL")
        self.assertIn("Only one component can be the examination", self.message(response))

    def test_putting_one_in_use_retires_the_one_for_the_same_programme_and_branch(self):
        draft = self.draft()
        response = self.activate(draft)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["status"], "IN_USE")
        self.assertEqual(self.status(draft), "IN_USE")
        self.assertEqual(self.status(self.old_school_wide), "RETIRED")

    def test_another_branchs_structure_and_another_programmes_are_left_alone(self):
        self.activate(self.draft())
        self.assertEqual(self.status(self.old_ikeja), "IN_USE")
        self.assertEqual(self.status(self.old_senior), "IN_USE")

    def test_a_branch_structure_retires_only_that_branchs_and_not_the_school_wide_one(self):
        draft = self.draft(branch=self.ikeja)
        self.activate(draft)
        self.assertEqual(self.status(self.old_ikeja), "RETIRED")
        self.assertEqual(self.status(self.old_school_wide), "IN_USE")
        self.assertEqual(self.status(draft), "IN_USE")

    def test_only_a_draft_is_put_in_use(self):
        response = self.activate(self.old_school_wide)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "NOT_A_DRAFT")
        self.assertIn("already in use", self.message(response))
        retired = self.make_structure(self.junior, status=SetupStatus.RETIRED, name="Gone")
        response = self.activate(retired)
        self.assertIn("retired", self.message(response))

    def test_the_replacement_and_the_retirement_are_both_audited(self):
        draft = self.draft()
        self.activate(draft)
        events = AuditEvent.objects.filter(entity_type="AssessmentStructure", module_key="GRADEBOOK")
        retired = events.get(entity_id=str(self.old_school_wide.pk))
        self.assertIn("was retired because New replaced it", retired.summary)
        put = events.get(entity_id=str(draft.pk), action_type="UPDATE")
        self.assertIn("was put in use for Junior Secondary, every branch", put.summary)

    def test_the_database_allows_one_in_use_per_programme_and_branch(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_structure(self.junior, name="Second in use")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_structure(self.junior, branch=self.ikeja, name="Second Ikeja")
        self.make_structure(self.junior, branch=self.lekki, name="Lekki in use")


class StructureLockTests(GradebookFixture):
    """A published class locks the structure it was made with; copying is the way out."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.locked = cls.make_structure(cls.junior, name="Used")
        cls.publish(cls.jss1a, cls.locked)

    def test_the_lock_names_the_class(self):
        rows = {r["name"]: r for r in self.data(self.get(self.admin, "gradebook-structures"))}
        row = rows["Used"]
        self.assertTrue(row["locked"])
        self.assertEqual(
            row["lock_reason"],
            "JSS1 A published First Term results made with this structure, so it can no "
            "longer be changed. Copy it to start a new draft.",
        )

    def test_the_lock_counts_other_classes(self):
        self.publish(self.shared, self.locked)
        self.publish(self.jss1b, self.locked)
        row = self.data(self.get(self.admin, "gradebook-structure", pk=self.locked.pk))
        self.assertIn("JSS1 A and 2 other classes published", row["lock_reason"])

    def test_an_edit_is_refused(self):
        response = self.patch(
            self.admin, "gradebook-structure", {"name": "Changed"}, pk=self.locked.pk,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "USED_BY_PUBLISHED_RESULT")
        self.assertIn("JSS1 A published First Term results", self.message(response))

    def test_putting_it_in_use_again_and_deleting_it_are_refused(self):
        response = self.post(self.admin, "gradebook-structure-activate", pk=self.locked.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "USED_BY_PUBLISHED_RESULT")
        response = self.delete(self.admin, "gradebook-structure", pk=self.locked.pk)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(AssessmentStructure.objects.filter(pk=self.locked.pk).exists())

    def test_copying_is_allowed_and_putting_the_copy_in_use_replaces_it(self):
        copy = self.data(self.post(self.admin, "gradebook-structure-copy", pk=self.locked.pk))
        rows = copy["components"]
        rows[0]["max_score"] = 30
        rows[2]["max_score"] = 50
        edited = self.patch(
            self.admin, "gradebook-structure", {"components": rows}, pk=copy["id"],
        )
        self.assertEqual(edited.status_code, 200, edited.data)
        response = self.post(self.admin, "gradebook-structure-activate", pk=copy["id"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(AssessmentStructure.objects.get(pk=self.locked.pk).status, "RETIRED")
        self.assertEqual(AssessmentStructure.objects.get(pk=copy["id"]).status, "IN_USE")

    def test_a_class_that_has_not_published_does_not_lock_it(self):
        free = self.make_structure(self.senior, name="Free")
        self.make_class_term(self.sss1a, structure=free)
        row = self.data(self.get(self.admin, "gradebook-structure", pk=free.pk))
        self.assertFalse(row["locked"])


class StructureMarksTests(GradebookFixture):
    """An edit of a structure in use must not orphan a mark already entered."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.structure = cls.make_structure(cls.junior, name="In use")
        cls.ca1, cls.ca2, cls.exam = list(cls.structure.components.all())
        cls.class_term = cls.make_class_term(cls.jss1a, structure=cls.structure)
        cls.sheet = ScoreSheet.all_objects.create(
            tenant=cls.tenant, class_term=cls.class_term, subject=cls.maths,
        )
        cls.make_score(cls.chiamaka, cls.ca1, mark=18)
        cls.make_score(cls.tunde, cls.ca1, mark=12)
        cls.make_score(cls.tunde, cls.exam, marker="ABS")

    @classmethod
    def make_score(cls, student, component, mark=None, marker=None):
        return Score.all_objects.create(
            tenant=cls.tenant, sheet=cls.sheet, student=student, component=component,
            mark=mark, marker=marker,
        )

    def rows(self, **max_scores):
        return [
            {"id": c.pk, "name": c.name, "short_name": c.short_name,
             "max_score": max_scores.get(c.short_name, c.max_score), "is_exam": c.is_exam}
            for c in (self.ca1, self.ca2, self.exam)
        ]

    def edit(self, rows):
        return self.patch(
            self.admin, "gradebook-structure", {"components": rows}, pk=self.structure.pk,
        )

    def test_lowering_a_maximum_below_a_mark_is_refused_naming_the_sheet(self):
        response = self.edit(self.rows(CA1=15, EXAM=65))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "WOULD_INVALIDATE_MARKS")
        self.assertEqual(
            self.message(response),
            "JSS1 A Mathematics (First Term) already has a mark of 18 in 1st CA, which is "
            "above the new maximum of 15. Keep the maximum at 18 or more, or correct those "
            "marks first.",
        )
        self.assertEqual(response.data["error"]["detail"]["sheet"], self.sheet.pk)
        self.assertEqual(self.ca1.__class__.objects.get(pk=self.ca1.pk).max_score, 20)

    def test_lowering_a_maximum_to_the_highest_mark_is_allowed(self):
        response = self.edit(self.rows(CA1=18, EXAM=62))
        self.assertEqual(response.status_code, 200, response.data)

    def test_removing_a_component_with_marks_is_refused(self):
        rows = [r for r in self.rows() if r["short_name"] != "CA1"]
        rows[0]["max_score"] = 40
        response = self.edit(rows)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "WOULD_INVALIDATE_MARKS")
        self.assertIn("already has marks in 1st CA, so it cannot be removed", self.message(response))

    def test_removing_a_component_holding_only_a_marker_is_refused_too(self):
        rows = [r for r in self.rows() if r["short_name"] != "EXAM"]
        rows[0]["max_score"] = 50
        rows[1]["max_score"] = 50
        response = self.edit(rows)
        self.assertEqual(self.code(response), "WOULD_INVALIDATE_MARKS")
        self.assertIn("Examination", self.message(response))

    def test_a_component_with_no_marks_can_be_removed_and_one_added(self):
        rows = [r for r in self.rows() if r["short_name"] != "CA2"]
        rows[0]["max_score"] = 20
        rows[1]["max_score"] = 60
        rows.append({"name": "Project", "short_name": "PRJ", "max_score": 20, "is_exam": False})
        response = self.edit(rows)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [c["short_name"] for c in self.data(response)["components"]],
            ["CA1", "EXAM", "PRJ"],
        )

    def test_a_blank_cell_row_with_neither_mark_nor_marker_does_not_block(self):
        Score.all_objects.filter(component=self.ca2).delete()
        self.make_score(self.kelechi, self.ca2)
        rows = [r for r in self.rows() if r["short_name"] != "CA2"]
        rows[0]["max_score"] = 20
        rows[1]["max_score"] = 60
        rows.append({"name": "Project", "short_name": "PRJ", "max_score": 20, "is_exam": False})
        response = self.edit(rows)
        self.assertEqual(response.status_code, 200, response.data)

    def test_marks_on_another_structures_class_do_not_block_this_one(self):
        other = self.make_structure(self.senior, name="Other")
        component = other.components.first()
        sheet = ScoreSheet.all_objects.create(
            tenant=self.tenant, class_term=self.make_class_term(self.sss1a, structure=other),
            subject=self.maths,
        )
        Score.all_objects.create(
            tenant=self.tenant, sheet=sheet, student=self.sade, component=component, mark=20,
        )
        rows = [
            {"id": c.pk, "name": c.name, "short_name": c.short_name,
             "max_score": 25 if c.pk == component.pk else c.max_score, "is_exam": c.is_exam}
            for c in other.components.all()
        ]
        rows[2]["max_score"] = 55
        response = self.patch(
            self.admin, "gradebook-structure", {"components": rows}, pk=other.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)


class SingleBranchSchoolTests(GradebookFixture):
    """At a school with one branch the whole lifecycle works and no screen names a branch."""

    def test_create_activate_and_resolve_without_ever_choosing_a_branch(self):
        from ..services import resolve

        created = self.post(self.solo_admin, "gradebook-structures", {
            "name": "Basic", "program": self.solo_program.pk,
            "components": self.components_body(),
        })
        self.assertEqual(created.status_code, 201, created.data)
        row = self.data(created)
        self.assertNotIn("scope_label", row)
        self.assertIsNone(row["branch"])

        activated = self.post(
            self.solo_admin, "gradebook-structure-activate", pk=row["id"],
        )
        self.assertEqual(activated.status_code, 200, activated.data)
        structure = resolve.structure_for(self.solo, self.solo_class)
        self.assertEqual(structure.pk, row["id"])

    def test_the_second_one_in_use_replaces_the_first(self):
        first = self.make_structure(self.solo_program, tenant=self.solo, name="First")
        second = self.make_structure(
            self.solo_program, tenant=self.solo, status=SetupStatus.DRAFT, name="Second",
        )
        response = self.post(self.solo_admin, "gradebook-structure-activate", pk=second.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(AssessmentStructure.objects.get(pk=first.pk).status, "RETIRED")

    def test_a_teacher_at_the_school_cannot_change_setup(self):
        response = self.post(self.solo_teacher, "gradebook-structures", {
            "name": "Basic", "program": self.solo_program.pk,
            "components": self.components_body(),
        })
        self.assertEqual(response.status_code, 403)
