"""Loading a school year's calendar from a spreadsheet.

The first dataset in the platform a school may import for itself, so these tests
carry two jobs at once. The ordinary one is that a well-filled file produces the
right entries. The other, and the reason this file is long, is that **the
validator and the executor must read every row identically**: they are separate
passes minutes apart, and an import goes wrong quietly when a file passes with a
green tick and then imports something else. Every rule below is asserted on both
passes wherever both can see it.
"""
from __future__ import annotations

import datetime as dt

from vs_import_data.models import (
    ImportBatch,
    ImportJob,
    ImportJobRowResult,
    ImportJobStatusChoices,
    ImportRowActionChoices,
    ImportTemplate,
    ImportTemplateColumn,
)
from vs_import_data.services.import_executor import execute_dataset_handler
from vs_import_data.services.reversers import REFUSED, REVERTED, reverse_row

from ..imports import COLUMNS, resolve_row, validate_calendar_events_import_batch
from ..models import CalendarEvent, CalendarEventAudience
from .base import _Base, _SingleBranchBase

HEADERS = {
    "name": "Event Name",
    "event_type": "Event Type",
    "start_date": "Start Date",
    "end_date": "End Date",
    "branch": "Branch",
    "closes_school": "Closes School",
    "description": "Description",
    "applies_to": "Applies To",
}


def row(**overrides) -> dict:
    """One uploaded row, keyed by the header a school actually sees."""
    values = {
        "name": "Mid-Term Break",
        "event_type": "Mid-term break",
        "start_date": "2025-11-03",
        "end_date": "2025-11-07",
        "branch": "",
        "closes_school": "Yes",
        "description": "",
        "applies_to": "",
    }
    values.update(overrides)
    return {HEADERS[k]: v for k, v in values.items()}


class _ImportMixin:
    """A seeded template and a batch to hang rows on."""

    @classmethod
    def make_template(cls):
        """The real template, built from the seed command's own definition.

        Not a hand-written stub. The whole risk in this dataset is the template
        and the handler drifting apart, and a stub would hide exactly that.
        """
        from core.management.commands.seed_import import TEMPLATES_BY_DATASET_TYPE

        entry = TEMPLATES_BY_DATASET_TYPE["calendar_events"][0]
        template = ImportTemplate.objects.create(**entry["template"])
        for column in entry["columns"]:
            ImportTemplateColumn.objects.create(template=template, **column)
        return template

    def make_batch(self, rows, *, branch=None, tenant=None, user=None):
        return ImportBatch.all_objects.create(
            tenant=tenant or self.tenant,
            branch=branch,
            uploaded_by=user or self.admin,
            template=self.template,
            dataset_type="calendar_events",
            file="",
            preview_rows=rows,
            total_rows=len(rows),
        )

    def issues(self, rows, **kwargs):
        return validate_calendar_events_import_batch(self.make_batch(rows, **kwargs))

    def errors(self, rows, **kwargs):
        return [i for i in self.issues(rows, **kwargs) if i["severity"] == "error"]

    def warnings(self, rows, **kwargs):
        return [i for i in self.issues(rows, **kwargs) if i["severity"] == "warning"]

    def run_row(self, raw, *, branch=None, tenant=None, user=None):
        """Execute one row the way the engine does, mapping headers first."""
        from vs_import_data.services.import_executor import map_row_to_payload

        batch = self.make_batch([raw], branch=branch, tenant=tenant, user=user)
        return execute_dataset_handler(
            import_batch=batch,
            payload=map_row_to_payload(batch, raw),
            queued_by=user or self.admin,
        )


class TemplateAgreementTests(_ImportMixin, _Base):
    """The file the school downloads and the code that reads it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.template = cls.make_template()

    def test_template_columns_match_the_handler(self):
        """Every column the template ships is one the handler reads.

        The failure this catches is silent in both directions. A column added
        to the template and not to the handler is a column a school fills in
        that changes nothing; a field the handler reads with no column behind it
        is always blank, so a required value looks missing on every row of a
        correctly filled file.
        """
        seeded = tuple(
            c.target_field
            for c in self.template.columns.order_by("column_order")
        )
        self.assertEqual(seeded, COLUMNS)

    def test_the_sample_row_is_one_a_school_could_import(self):
        """The sample is the school's worked example, so it has to be valid.

        Its dates are next year's, so this checks the shape rather than the
        calendar: type resolves, dates parse, and nothing is malformed.
        """
        sample = self.template.sample_row_data
        resolved = resolve_row(
            {c.target_field: sample.get(c.column_name)
             for c in self.template.columns.all()},
            tenant=self.tenant, session=self.year,
            batch_branch=None, multi_branch=True,
        )
        complaints = [
            i.code for i in resolved.issues
            if i.severity == "error" and i.code != "business_rule"
        ]
        self.assertEqual(complaints, [])


class ValidationTests(_ImportMixin, _Base):
    """Brightfield: two branches, one active year, three terms."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.template = cls.make_template()

    # ── the file that is right ────────────────────────────────────────────

    def test_a_good_file_passes(self):
        self.assertEqual(self.errors([row()]), [])

    def test_the_stored_code_is_accepted_as_well_as_the_label(self):
        """A school reading the API sees MIDTERM_BREAK; the template says
        Mid-term break. Refusing either would be pedantry."""
        self.assertEqual(self.errors([row(event_type="MIDTERM_BREAK")]), [])
        self.assertEqual(self.errors([row(event_type="mid-term break")]), [])

    def test_a_one_day_entry_repeats_its_date(self):
        self.assertEqual(
            self.errors([row(start_date="2025-10-01", end_date="2025-10-01")]),
            [],
        )

    # ── the file that is wrong ────────────────────────────────────────────

    def test_an_unknown_type_names_the_ones_that_work(self):
        found = self.errors([row(event_type="Half Term")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "invalid_choice")
        self.assertIn("Mid-term break", found[0]["message"])
        self.assertEqual(found[0]["column_name"], "Event Type")

    def test_a_date_outside_the_year_is_refused(self):
        """2025/2026 runs 8 Sep 2025 to 17 Jul 2026, so an August entry
        belongs to a year this file is not importing into."""
        found = self.errors([row(start_date="2025-08-01", end_date="2025-08-02")])
        self.assertEqual(len(found), 1)
        self.assertIn("2025/2026", found[0]["message"])

    def test_an_entry_that_ends_before_it_starts_is_refused(self):
        found = self.errors([row(start_date="2025-11-07", end_date="2025-11-03")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "business_rule")

    def test_a_date_that_is_not_a_date_says_so(self):
        found = self.errors([row(start_date="3rd November")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "invalid_format")
        self.assertIn("YYYY-MM-DD", found[0]["message"])

    def test_a_missing_name_is_refused(self):
        found = self.errors([row(name="")])
        self.assertEqual([i["code"] for i in found], ["required_value_missing"])

    def test_closes_school_only_takes_yes_or_no(self):
        self.assertEqual(self.errors([row(closes_school="")]), [])
        self.assertEqual(self.errors([row(closes_school="no")]), [])
        found = self.errors([row(closes_school="maybe")])
        self.assertEqual([i["code"] for i in found], ["invalid_choice"])

    # ── who it applies to ─────────────────────────────────────────────────

    def test_a_name_that_resolves_to_nothing_is_refused_not_ignored(self):
        """The Mrs Adeyemi case, arriving by spreadsheet.

        Lekki uploads a Speech Day narrowed to 'Primary 4 (Lekki)', which is not
        what the level is called. Importing it for everybody instead would take
        a teaching day off JSS1's three classes for the rest of the year, and
        nobody would find out until somebody queried the attendance figures.
        """
        found = self.errors([row(applies_to="Primary 4 (Lekki)")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "cross_reference_missing")
        self.assertEqual(found[0]["column_name"], "Applies To")
        self.assertIn("Primary 4 (Lekki)", found[0]["message"])

    def test_a_level_and_a_class_both_resolve(self):
        self.assertEqual(self.errors([row(applies_to="JSS1; JSS1 A")]), [])

    def test_one_bad_name_among_good_ones_is_still_refused(self):
        found = self.errors([row(applies_to="JSS1; Nursery 2")])
        self.assertEqual(len(found), 1)
        self.assertIn("Nursery 2", found[0]["message"])

    def test_a_branch_entry_cannot_narrow_to_another_branch(self):
        """An Ikeja class on a Lekki entry shows on Ikeja's calendar because of
        the class and not on it because of the branch."""
        found = self.errors([row(branch="Lekki Branch", applies_to="SSS2 Science")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "business_rule")
        self.assertIn("Lekki Branch", found[0]["message"])

    # ── branches ──────────────────────────────────────────────────────────

    def test_a_branch_name_resolves_case_insensitively(self):
        self.assertEqual(self.errors([row(branch="ikeja branch")]), [])

    def test_an_unknown_branch_is_refused(self):
        found = self.errors([row(branch="Yaba Branch")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "cross_reference_missing")

    def test_another_schools_branch_does_not_resolve(self):
        """Sunrise's Main Branch is a real branch and not this school's.

        Resolving it would let one school post entries onto another's calendar,
        which is the whole reason branch lookup is scoped to the batch's tenant.
        """
        found = self.errors([row(branch="Main Branch")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "cross_reference_missing")

    def test_a_branch_scoped_upload_cannot_write_another_branchs_calendar(self):
        found = self.errors([row(branch="Ikeja Branch")], branch=self.lekki)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "business_rule")
        self.assertIn("Lekki Branch", found[0]["message"])

    def test_a_branch_scoped_upload_defaults_every_row_to_its_branch(self):
        resolved = resolve_row(
            {"name": "Founder's Day", "event_type": "School event",
             "start_date": "2025-11-03", "end_date": "2025-11-03"},
            tenant=self.tenant, session=self.year,
            batch_branch=self.lekki, multi_branch=True,
        )
        self.assertEqual(resolved.branch, self.lekki)

    # ── duplicates ────────────────────────────────────────────────────────

    def test_the_same_entry_twice_in_one_file_is_refused(self):
        found = self.errors([row(), row()])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "duplicate_record")
        self.assertEqual(found[0]["row_number"], 2)

    def test_an_entry_already_on_the_calendar_is_a_warning(self):
        """Re-uploading a corrected file is normal, and the second upload must
        not refuse the rows that already went in - it skips them."""
        CalendarEvent.objects.create(
            tenant=self.tenant, session=self.year, branch=None,
            name="Mid-Term Break", event_type="MIDTERM_BREAK",
            start_date=dt.date(2025, 11, 3), end_date=dt.date(2025, 11, 7),
        )
        self.assertEqual(self.errors([row()]), [])
        found = self.warnings([row()])
        self.assertEqual([i["code"] for i in found], ["duplicate_record"])

    # ── warnings that do not block ────────────────────────────────────────

    def test_a_date_between_terms_is_warned_and_kept(self):
        """The December break sits between First and Second Term. Refusing it
        would make the calendar wrong to protect a rule nobody asked for."""
        found = self.issues([row(start_date="2025-12-24", end_date="2025-12-26")])
        self.assertEqual([i["severity"] for i in found], ["warning"])
        self.assertIn("outside every term", found[0]["message"])

    def test_an_overlapping_entry_of_the_same_kind_is_warned_and_kept(self):
        CalendarEvent.objects.create(
            tenant=self.tenant, session=self.year, branch=None,
            name="Autumn Break", event_type="MIDTERM_BREAK",
            start_date=dt.date(2025, 11, 5), end_date=dt.date(2025, 11, 10),
        )
        found = self.warnings([row()])
        self.assertEqual([i["code"] for i in found], ["business_rule"])
        self.assertIn("Autumn Break", found[0]["message"])

    # ── the year ──────────────────────────────────────────────────────────

    def test_an_archived_year_refuses_the_whole_file(self):
        """Told once, before the school fills in three hundred rows."""
        self.year.status = "ARCHIVED"
        self.year.save(update_fields=["status"])
        try:
            found = self.errors([row()])
            self.assertEqual(len(found), 1)
            self.assertIsNone(found[0]["row_number"])
            self.assertIn("archived", found[0]["message"].lower())
        finally:
            self.year.status = "ACTIVE"
            self.year.save(update_fields=["status"])


class SingleBranchValidationTests(_ImportMixin, _SingleBranchBase):
    """Sunrise runs one branch, so the branch column has no meaning here."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.template = cls.make_template()

    def test_a_branch_name_is_refused_rather_than_ignored(self):
        """Ignoring it would look identical to it working.

        Sunrise fills in 'Main Branch' on forty rows because the column is
        there, the import succeeds, and the school believes it has forty
        branch entries. It has forty school-wide ones and no way to tell.
        """
        found = self.errors([row(start_date="2025-11-03", end_date="2025-11-07",
                                 branch="Main Branch")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["code"], "business_rule")
        self.assertIn("one branch", found[0]["message"])

    def test_a_blank_branch_column_is_the_ordinary_case(self):
        self.assertEqual(
            self.errors([row(start_date="2025-11-03", end_date="2025-11-07")]),
            [],
        )


class ExecutionTests(_ImportMixin, _Base):
    """What the handler writes, given a row the validator passed."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.template = cls.make_template()

    def test_it_creates_the_entry_in_the_batchs_own_year(self):
        result = self.run_row(row(description="Resumes Monday."))
        self.assertEqual(result.action, ImportRowActionChoices.CREATE)

        event = CalendarEvent.objects.get(pk=result.instance.pk)
        self.assertEqual(event.tenant_id, self.tenant.pk)
        self.assertEqual(event.session_id, self.year.pk)
        self.assertEqual(event.name, "Mid-Term Break")
        self.assertEqual(event.event_type, "MIDTERM_BREAK")
        self.assertEqual(event.start_date, dt.date(2025, 11, 3))
        self.assertEqual(event.end_date, dt.date(2025, 11, 7))
        self.assertTrue(event.closes_school)
        self.assertIsNone(event.branch_id)
        self.assertEqual(event.created_by_id, self.admin.pk)

    def test_it_writes_the_audience_it_was_given(self):
        result = self.run_row(row(applies_to="JSS1; JSS1 A"))
        rows = CalendarEventAudience.objects.filter(event=result.instance)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            {r.level_id for r in rows if r.level_id}, {self.jss1.pk},
        )
        self.assertEqual(
            {r.school_class_id for r in rows if r.school_class_id}, {self.jss1a.pk},
        )

    def test_a_blank_audience_means_everybody(self):
        result = self.run_row(row())
        self.assertFalse(
            CalendarEventAudience.objects.filter(event=result.instance).exists(),
        )

    def test_it_puts_the_entry_at_the_branch_the_row_names(self):
        result = self.run_row(row(branch="Ikeja Branch"))
        self.assertEqual(result.instance.branch_id, self.ikeja.pk)

    def test_an_entry_already_there_is_skipped_not_repeated(self):
        first = self.run_row(row())
        second = self.run_row(row())
        self.assertEqual(second.action, ImportRowActionChoices.SKIP)
        self.assertIsNone(second.instance)
        self.assertEqual(
            CalendarEvent.objects.filter(tenant=self.tenant, name="Mid-Term Break").count(),
            1,
        )
        self.assertIsNotNone(first.instance)

    def test_a_row_the_validator_would_refuse_fails_rather_than_writing(self):
        """The executor does not trust that validation ran.

        The file can change between the two passes, and a row that reaches here
        with a fault fails with its own reason and writes nothing.
        """
        with self.assertRaises(ValueError) as caught:
            self.run_row(row(applies_to="Nursery 2"))
        self.assertIn("Nursery 2", str(caught.exception))
        self.assertFalse(
            CalendarEvent.objects.filter(tenant=self.tenant, name="Mid-Term Break").exists(),
        )

    def test_the_uploading_school_is_the_only_school_it_can_write_to(self):
        """A batch belonging to Sunrise writes Sunrise's calendar.

        The handler takes its tenant from the batch and the template has no
        school column, so there is nothing in the file for a crafted row to
        name a different school with.
        """
        result = self.run_row(
            row(start_date="2025-11-03", end_date="2025-11-07"),
            tenant=self.other.tenant,
            user=self.admin,
        )
        self.assertEqual(result.instance.tenant_id, self.other.tenant.pk)
        self.assertEqual(result.instance.session_id, self.other_year.pk)
        self.assertFalse(
            CalendarEvent.objects.filter(tenant=self.tenant).exists(),
        )


class RollbackTests(_ImportMixin, _Base):
    """Undoing a calendar import, one row at a time."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.template = cls.make_template()

    def imported(self, raw=None, **kwargs):
        """One executed row, plus the row result a rollback would read."""
        from vs_import_data.services.import_executor import map_row_to_payload

        raw = raw or row()
        batch = self.make_batch([raw], **kwargs)
        payload = map_row_to_payload(batch, raw)
        result = execute_dataset_handler(
            import_batch=batch, payload=payload, queued_by=self.admin,
        )
        job = ImportJob.objects.create(
            import_batch=batch, queued_by=self.admin,
            status=ImportJobStatusChoices.SUCCEEDED, total_rows=1,
        )
        return result.instance, ImportJobRowResult.objects.create(
            job=job, row_number=1, action=result.action,
            target_model=result.target_model,
            target_object_pk=str(result.instance.pk) if result.instance else "",
            row_payload=raw, normalized_payload=payload,
        )

    def test_a_rollback_removes_the_entry_and_its_audience(self):
        event, row_result = self.imported(row(applies_to="JSS1"))
        self.assertTrue(CalendarEventAudience.objects.filter(event=event).exists())

        outcome = reverse_row(row_result)
        self.assertEqual(outcome.status, REVERTED)
        self.assertFalse(CalendarEvent.objects.filter(pk=event.pk).exists())
        # CASCADE, so one delete takes the narrowing with it.
        self.assertFalse(
            CalendarEventAudience.objects.filter(event_id=event.pk).exists(),
        )

    def test_it_refuses_when_the_id_now_names_something_else(self):
        """An id alone is not an identity: rows outlive the objects they name.

        The row recorded 'Mid-Term Break'. If id 12 is now called something
        else, it is not the entry this row created and deleting it would
        destroy a school's own work.
        """
        event, row_result = self.imported()
        CalendarEvent.objects.filter(pk=event.pk).update(name="Sports Day")

        outcome = reverse_row(row_result)
        self.assertEqual(outcome.status, REFUSED)
        self.assertIn("Sports Day", outcome.message)
        self.assertTrue(CalendarEvent.objects.filter(pk=event.pk).exists())

    def test_it_refuses_to_take_an_exam_timetable_with_it(self):
        """The events API refuses this same delete, so a rollback that went
        ahead would be a way round a rule the API holds."""
        from ..models import Exam

        event, row_result = self.imported(
            row(name="First Term Examinations", event_type="Exam period"),
        )
        Exam.objects.create(
            tenant=self.tenant, calendar_event=event,
            name="First Term Examinations",
        )

        outcome = reverse_row(row_result)
        self.assertEqual(outcome.status, REFUSED)
        self.assertIn("exam timetable", outcome.message)
        self.assertTrue(CalendarEvent.objects.filter(pk=event.pk).exists())

    def test_it_refuses_an_entry_belonging_to_another_school(self):
        """Ownership is checked against the batch, which is right for this
        dataset precisely because the batch belongs to the school the row
        wrote into."""
        event, row_result = self.imported()
        CalendarEvent.objects.filter(pk=event.pk).update(tenant=self.other.tenant)

        outcome = reverse_row(row_result)
        self.assertEqual(outcome.status, REFUSED)
        self.assertIn("different school", outcome.message)

    def test_a_skipped_row_is_not_reversed(self):
        """It skipped because the entry was already there, so the entry is the
        school's own and not the import's to delete."""
        event, _ = self.imported()
        _, skipped = self.imported()
        self.assertEqual(skipped.action, ImportRowActionChoices.SKIP)

        outcome = reverse_row(skipped)
        self.assertEqual(outcome.status, REFUSED)
        self.assertIn("not a creation", outcome.message)
        self.assertTrue(CalendarEvent.objects.filter(pk=event.pk).exists())
