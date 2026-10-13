"""Grading scales: the check on every read, the lifecycle and the lock on a published snapshot."""
from __future__ import annotations

from vs_audit.models import AuditEvent

from ..constants import SetupStatus
from ..models import GradeBand, GradingScale, StudentTermResult
from .base import JUNIOR_BANDS, SENIOR_BANDS, GradebookFixture


class ScaleAccessTests(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.scale = cls.make_scale(cls.junior, status=SetupStatus.DRAFT)

    def test_a_caller_without_the_view_key_is_refused_every_read(self):
        for user in (self.nobody, self.updater, self.eze):
            with self.subTest(user=user.email):
                self.assertEqual(self.get(user, "gradebook-scales").status_code, 403)
                self.assertEqual(
                    self.get(user, "gradebook-scale", pk=self.scale.pk).status_code, 403,
                )

    def test_a_caller_with_only_the_view_key_cannot_write(self):
        pk = self.scale.pk
        body = {
            "name": "New", "program": self.junior.pk, "bands": self.bands_body(),
        }
        for response in (
            self.post(self.viewer, "gradebook-scales", body),
            self.patch(self.viewer, "gradebook-scale", {"name": "x"}, pk=pk),
            self.delete(self.viewer, "gradebook-scale", pk=pk),
            self.post(self.viewer, "gradebook-scale-copy", pk=pk),
            self.post(self.viewer, "gradebook-scale-activate", pk=pk),
        ):
            self.assertEqual(response.status_code, 403)

    def test_a_branch_bound_caller_is_refused_every_write(self):
        pk = self.scale.pk
        for response in (
            self.post(self.lekki_admin, "gradebook-scales", {
                "name": "New", "program": self.junior.pk, "bands": self.bands_body(),
            }),
            self.patch(self.lekki_admin, "gradebook-scale", {"name": "x"}, pk=pk),
            self.delete(self.lekki_admin, "gradebook-scale", pk=pk),
            self.post(self.lekki_admin, "gradebook-scale-copy", pk=pk),
            self.post(self.lekki_admin, "gradebook-scale-activate", pk=pk),
        ):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.code(response), "SHARED_RECORD_READ_ONLY")

    def test_another_schools_scale_is_a_404(self):
        theirs = self.make_scale(self.solo_program, tenant=self.solo, name="Theirs")
        self.assertEqual(self.get(self.admin, "gradebook-scale", pk=theirs.pk).status_code, 404)
        self.assertEqual(
            self.post(self.admin, "gradebook-scale-activate", pk=theirs.pk).status_code, 404,
        )
        self.assertNotIn(
            "Theirs", {r["name"] for r in self.data(self.get(self.admin, "gradebook-scales"))},
        )


class ScaleReadTests(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.junior_scale = cls.make_scale(cls.junior, name="Junior")
        cls.senior_scale = cls.make_scale(cls.senior, name="Senior", bands=SENIOR_BANDS)
        cls.bad = cls.make_scale(
            cls.senior, status=SetupStatus.DRAFT, name="Gappy",
            bands=((10, "A", "", False), (50, "B", "", False)),
        )

    def rows(self, **query):
        return {r["name"]: r for r in self.data(self.get(self.admin, "gradebook-scales", query))}

    def test_a_sound_scale_reads_with_its_bands_and_an_ok_check(self):
        row = self.rows()["Senior"]
        self.assertEqual(
            row["check"], {"ok": True, "message": "No gap, no overlap, 0 to 100 covered."},
        )
        self.assertEqual(len(row["bands"]), 9)
        self.assertEqual(
            row["bands"][0],
            {"id": row["bands"][0]["id"], "min_score": 75, "grade": "A1",
             "remark": "Excellent", "is_fail": False, "order": 1},
        )
        self.assertEqual(row["bands"][-1]["is_fail"], True)
        self.assertEqual(row["scope_label"], "School-wide")
        self.assertFalse(row["locked"])

    def test_an_unsound_draft_reads_with_the_reason_in_the_check(self):
        row = self.rows()["Gappy"]
        self.assertFalse(row["check"]["ok"])
        self.assertEqual(
            row["check"]["message"],
            "The lowest band starts at 10, so marks below it have no grade. "
            "Make the lowest band start at 0.",
        )

    def test_the_check_is_on_the_detail_read_too(self):
        row = self.data(self.get(self.admin, "gradebook-scale", pk=self.bad.pk))
        self.assertFalse(row["check"]["ok"])

    def test_filters(self):
        self.assertEqual(set(self.rows(program=self.junior.pk)), {"Junior"})
        self.assertEqual(set(self.rows(status="DRAFT")), {"Gappy"})

    def test_a_school_with_one_branch_names_no_branch(self):
        self.make_scale(self.solo_program, tenant=self.solo, name="Only")
        rows = self.data(self.get(self.solo_admin, "gradebook-scales"))
        self.assertNotIn("scope_label", rows[0])
        self.assertEqual(self.get(self.solo_admin, "gradebook-scales").status_code, 200)


class ScaleCreateAndEditTests(GradebookFixture):
    def create(self, **overrides):
        body = {
            "name": "Termly", "program": self.junior.pk, "bands": self.bands_body(),
            **overrides,
        }
        return self.post(self.admin, "gradebook-scales", body)

    def test_creates_a_draft_with_its_bands_in_order(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        row = self.data(response)
        self.assertEqual(row["status"], "DRAFT")
        self.assertEqual([b["order"] for b in row["bands"]], [1, 2, 3, 4, 5])
        self.assertTrue(row["check"]["ok"])
        event = AuditEvent.objects.get(entity_type="GradingScale", entity_id=str(row["id"]))
        self.assertEqual((event.module_key, event.action_type), ("GRADEBOOK", "CREATE"))

    def test_a_draft_may_be_unsound(self):
        response = self.create(bands=[
            {"min_score": 10, "grade": "A"}, {"min_score": 10, "grade": "B"},
        ])
        self.assertEqual(response.status_code, 201)
        self.assertIn("Two bands start at 10", self.data(response)["check"]["message"])

    def test_each_band_is_validated(self):
        base = {"min_score": 50, "grade": "C", "remark": "Fine", "is_fail": False}
        cases = {
            "negative": ({**base, "min_score": -1}, "cannot be below 0"),
            "over": ({**base, "min_score": 101}, "cannot be above 100"),
            "no grade": ({**base, "grade": ""}, "may not be blank"),
            "long grade": ({**base, "grade": "G" * 11}, "no more than 10"),
            "decimal": ({**base, "min_score": 50.5}, "whole number"),
        }
        for label, (row, expected) in cases.items():
            with self.subTest(case=label):
                response = self.create(bands=[row])
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, str(response.data))

    def test_a_create_needs_bands(self):
        response = self.post(self.admin, "gradebook-scales", {
            "name": "x", "program": self.junior.pk,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Add at least one band.", str(response.data))

    def test_bands_are_replaced_keeping_the_ids_of_those_named(self):
        scale = self.make_scale(self.junior, status=SetupStatus.DRAFT)
        bands = list(scale.bands.all())
        rows = [
            {"id": b.pk, "min_score": b.min_score, "grade": b.grade, "remark": b.remark,
             "is_fail": b.is_fail}
            for b in bands
        ]
        rows[0]["grade"] = "A+"
        rows.pop()
        rows.append({"min_score": 0, "grade": "E", "remark": "Poor", "is_fail": True})
        response = self.patch(self.admin, "gradebook-scale", {"bands": rows}, pk=scale.pk)
        self.assertEqual(response.status_code, 200, response.data)
        result = self.data(response)["bands"]
        self.assertEqual([b["grade"] for b in result], ["A+", "B", "C", "D", "E"])
        self.assertEqual(result[0]["id"], bands[0].pk)
        self.assertFalse(GradeBand.objects.filter(pk=bands[-1].pk).exists())

    def test_an_edit_that_leaves_one_in_use_unsound_is_refused(self):
        scale = self.make_scale(self.junior, name="In use")
        rows = [
            {"id": b.pk, "min_score": b.min_score + (5 if b.min_score == 0 else 0),
             "grade": b.grade, "remark": b.remark, "is_fail": b.is_fail}
            for b in scale.bands.all()
        ]
        response = self.patch(self.admin, "gradebook-scale", {"bands": rows}, pk=scale.pk)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "SCALE_GAP")
        self.assertIn("The lowest band starts at 5", self.message(response))

    def test_one_in_use_can_have_a_remark_reworded(self):
        scale = self.make_scale(self.junior, name="In use")
        rows = [
            {"id": b.pk, "min_score": b.min_score, "grade": b.grade,
             "remark": "Brilliant" if b.grade == "A" else b.remark, "is_fail": b.is_fail}
            for b in scale.bands.all()
        ]
        response = self.patch(self.admin, "gradebook-scale", {"bands": rows}, pk=scale.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data(response)["bands"][0]["remark"], "Brilliant")


class ScaleLifecycleTests(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.old = cls.make_scale(cls.junior, name="Old")
        cls.old_ikeja = cls.make_scale(cls.junior, branch=cls.ikeja, name="Old Ikeja")

    def status(self, scale):
        return GradingScale.objects.get(pk=scale.pk).status

    def draft(self, bands=JUNIOR_BANDS, **kwargs):
        return self.make_scale(self.junior, status=SetupStatus.DRAFT, name="New", bands=bands, **kwargs)

    def activate(self, scale):
        return self.post(self.admin, "gradebook-scale-activate", pk=scale.pk)

    def test_activation_retires_the_one_for_the_same_programme_and_branch_only(self):
        draft = self.draft()
        response = self.activate(draft)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.status(draft), "IN_USE")
        self.assertEqual(self.status(self.old), "RETIRED")
        self.assertEqual(self.status(self.old_ikeja), "IN_USE")

    def test_an_unsound_scale_is_refused_with_the_reason_the_editor_shows(self):
        cases = {
            "no zero": (((10, "A", "", False), (50, "B", "", False)), "The lowest band starts at 10"),
            "overlap": (((0, "F", "", True), (50, "C", "", False), (50, "D", "", False)),
                        "Two bands start at 50, so they overlap"),
            "all fails": (((0, "F", "", True), (40, "E", "", True)), "Every band is a fail"),
        }
        for label, (bands, expected) in cases.items():
            draft = self.draft(bands=bands)
            with self.subTest(case=label):
                response = self.activate(draft)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.code(response), "SCALE_GAP")
                self.assertIn(expected, self.message(response))
                self.assertEqual(self.status(draft), "DRAFT")
        self.assertEqual(self.status(self.old), "IN_USE")

    def test_only_a_draft_is_put_in_use(self):
        response = self.activate(self.old)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "NOT_A_DRAFT")

    def test_copy_is_a_draft_with_the_same_bands_and_remembers_its_source(self):
        response = self.post(self.admin, "gradebook-scale-copy", pk=self.old.pk)
        self.assertEqual(response.status_code, 201)
        row = self.data(response)
        self.assertEqual((row["status"], row["copied_from"]), ("DRAFT", self.old.pk))
        self.assertEqual(
            [(b["min_score"], b["grade"], b["is_fail"]) for b in row["bands"]],
            [(low, g, f) for low, g, _, f in JUNIOR_BANDS],
        )

    def test_only_a_draft_is_deleted(self):
        draft = self.draft()
        self.assertEqual(self.delete(self.admin, "gradebook-scale", pk=draft.pk).status_code, 200)
        self.assertEqual(self.delete(self.admin, "gradebook-scale", pk=self.old.pk).status_code, 409)

    def test_a_retired_scale_cannot_be_changed(self):
        retired = self.make_scale(self.junior, status=SetupStatus.RETIRED, name="Gone")
        response = self.patch(self.admin, "gradebook-scale", {"name": "x"}, pk=retired.pk)
        self.assertEqual(self.code(response), "NOT_A_DRAFT")

    def test_the_retirement_and_the_activation_are_audited(self):
        draft = self.draft()
        self.activate(draft)
        events = AuditEvent.objects.filter(entity_type="GradingScale")
        self.assertIn("was retired because New replaced it", events.get(entity_id=str(self.old.pk)).summary)
        self.assertIn("was put in use", events.get(entity_id=str(draft.pk), action_type="UPDATE").summary)

    def test_the_database_allows_one_in_use_per_programme_and_branch(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_scale(self.junior, name="Second")
        self.make_scale(self.junior, branch=self.lekki, name="Lekki")


class ScaleLockTests(GradebookFixture):
    """A scale is locked when a published student's snapshot carries its id."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.scale = cls.make_scale(cls.junior, name="Used")
        cls.free = cls.make_scale(cls.senior, name="Free", bands=SENIOR_BANDS)
        published = cls.publish(cls.jss1a, None)
        StudentTermResult.all_objects.create(
            tenant=cls.tenant, class_term=published, student=cls.chiamaka,
            snapshot={"scale_id": cls.scale.pk, "subjects": []}, version=1,
        )
        # A class still being compiled keeps a snapshot-less result, and an
        # unpublished class never locks, whatever its rows say.
        draft = cls.make_class_term(cls.jss1b)
        StudentTermResult.all_objects.create(
            tenant=cls.tenant, class_term=draft, student=cls.tunde,
            snapshot={"scale_id": cls.free.pk},
        )

    def test_the_lock_names_the_published_class(self):
        rows = {r["name"]: r for r in self.data(self.get(self.admin, "gradebook-scales"))}
        self.assertTrue(rows["Used"]["locked"])
        self.assertEqual(
            rows["Used"]["lock_reason"],
            "JSS1 A published First Term results made with this scale, so it can no longer "
            "be changed. Copy it to start a new draft.",
        )
        self.assertFalse(rows["Free"]["locked"])

    def test_an_edit_is_refused(self):
        response = self.patch(self.admin, "gradebook-scale", {"name": "x"}, pk=self.scale.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "USED_BY_PUBLISHED_RESULT")

    def test_putting_it_in_use_and_deleting_it_are_refused(self):
        response = self.post(self.admin, "gradebook-scale-activate", pk=self.scale.pk)
        self.assertEqual(self.code(response), "USED_BY_PUBLISHED_RESULT")
        self.assertEqual(
            self.delete(self.admin, "gradebook-scale", pk=self.scale.pk).status_code, 409,
        )

    def test_copying_is_allowed_and_the_copy_replaces_it_without_touching_the_published(self):
        copy = self.data(self.post(self.admin, "gradebook-scale-copy", pk=self.scale.pk))
        response = self.post(self.admin, "gradebook-scale-activate", pk=copy["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(GradingScale.objects.get(pk=self.scale.pk).status, "RETIRED")
        result = StudentTermResult.objects.get(student=self.chiamaka)
        self.assertEqual(result.snapshot["scale_id"], self.scale.pk)

    def test_a_scale_whose_snapshot_sits_in_an_unpublished_class_is_not_locked(self):
        response = self.patch(self.admin, "gradebook-scale", {"name": "Renamed"}, pk=self.free.pk)
        self.assertEqual(response.status_code, 200)
