"""The class grid: slot rules, branch containment, duplication, and status."""
from __future__ import annotations

import datetime as dt

from ..models import ClassTimetable, Period, PeriodType, PublishState, TimetableSlot
from .base import _Base


class SlotRuleTests(_Base):
    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")

    def test_a_lesson_in_a_break_is_refused(self):
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.brk.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_a1.pk,
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "SLOT_PERIOD_NOT_TEACHING",
        )

    def test_a_second_slot_in_the_same_cell_is_refused(self):
        body = {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_a1.pk,
        }
        self.assertEqual(
            self.post(self.admin, "calendar-slot-list", body).status_code, 201,
        )
        second = self.post(self.admin, "calendar-slot-list", body)
        # A cell holds one lesson. This is the class clash, and it is prevented
        # by the constraint rather than warned about.
        self.assertIn(second.status_code, (400, 409), second.data)

    def test_that_refusal_names_the_lesson_already_in_the_cell(self):
        """The most reachable refusal in the module, and it said nothing.

        It answered the platform's generic "A record with these details already
        exists", which on a grid means a cell refuses to fill and gives no
        reason. Two people editing one class's week hit this, and so does
        anyone who clicks a cell that was filled while they were looking at it -
        and the next thing they need to know is what is already in it.

        Found by sweeping the status-only assertions in this package after the
        same defect was fixed on the room and exam surfaces.
        """
        body = {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
        }
        self.assertEqual(
            self.post(self.admin, "calendar-slot-list", body).status_code, 201,
        )
        second = self.post(self.admin, "calendar-slot-list", body)
        self.assertEqual(second.data["error"]["code"], "CELL_ALREADY_FILLED")
        message = second.data["message"]
        self.assertIn(self.jss1a.name, message)
        self.assertIn(self.maths.name, message)
        self.assertIn(self.p1.label, message)
        self.assertIn("Monday", message)

    def test_editing_a_slot_in_place_is_not_a_clash_with_itself(self):
        """The row being edited must be excluded from its own check.

        Otherwise changing only the room on an existing lesson is refused for
        occupying the cell it already occupies.
        """
        created = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
        })
        self.assertEqual(created.status_code, 201, created.data)
        response = self.patch(
            self.admin, "calendar-slot-detail",
            {"room": self.room_a1.pk}, pk=created.data["data"]["id"],
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_period_from_another_day_is_refused(self):
        friday_only = Period.all_objects.create(
            tenant=self.tenant, session=self.year, day_of_week=5,
            order_index=1, label="Friday Period 1",
            period_type=PeriodType.LESSON,
            start_time=dt.time(8, 30), end_time=dt.time(9, 15),
        )
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": friday_only.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_a1.pk,
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "SLOT_PERIOD_WRONG_DAY")

    def test_a_slot_saves_without_a_teacher_or_a_room(self):
        """A school fills the subjects before it fills the people."""
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
        })
        self.assertEqual(response.status_code, 201, response.data)


class TeacherIdentityTests(_Base):
    """A teacher is a role grant, not a persona column."""

    def test_a_user_without_the_teacher_role_is_refused(self):
        from vs_rbac.tests.helpers import make_school_admin

        bursar = make_school_admin(
            None, email="bursar@brightfield.test", tenant=self.tenant,
        )
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": bursar.pk, "room": self.room_a1.pk,
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "NOT_A_TEACHING_USER")

    def test_an_admin_who_also_teaches_can_be_scheduled(self):
        """The principal who takes SSS3 Further Maths on Wednesdays.

        A persona column could not express this - it holds one value - and the
        FRD parks it as an open question for that reason. A role grant is
        additive, so it simply works.
        """
        self.make_teacher("principal@brightfield.test", "Adaeze", "Okonkwo")
        from vs_user.models import User

        principal = User.objects.get(email="principal@brightfield.test")
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": principal.pk, "room": self.room_a1.pk,
        })
        self.assertEqual(response.status_code, 201, response.data)

    def test_another_tenants_user_is_refused_the_same_way_as_a_non_teacher(self):
        """Indistinguishable, so the field cannot probe another school's ids."""
        from vs_rbac.tests.helpers import make_school_admin

        stranger = make_school_admin(
            None, email="them@sunrise.test", tenant=self.other.tenant,
        )
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": stranger.pk, "room": self.room_a1.pk,
        })
        self.assertIn(response.status_code, (400, 422), response.data)

    def test_a_teacher_pinned_to_lekki_can_be_scheduled_into_an_ikeja_room(self):
        """The criterion proving the picker is not narrowed by branch."""
        eze = self.make_teacher(
            "eze@brightfield.test", "Chukwuemeka", "Eze", branch=self.lekki,
        )
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.sss2.pk, "day_of_week": 4,
            "period": self.p1.pk, "subject": self.physics.pk,
            "teacher": eze.pk, "room": self.room_c1.pk,
        })
        self.assertEqual(response.status_code, 201, response.data)

    def test_the_picker_carries_no_email_address(self):
        self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")
        response = self.get(self.admin, "calendar-teacher-list")
        self.assertNotIn("eze@brightfield.test", str(response.data))

    def test_the_picker_promises_no_check_the_server_cannot_make(self):
        self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")
        response = self.get(self.admin, "calendar-teacher-list")
        blob = str(response.data).lower()
        for word in (
            "specialism", "available", "availability", "qualif",
            "suggest", "recommend", "max_load", "workload",
        ):
            self.assertNotIn(word, blob, f"the picker implies {word}")


class BranchContainmentTests(_Base):
    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")

    def test_a_lekki_class_cannot_use_an_ikeja_room(self):
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_c1.pk,
        })
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "ROOM_BRANCH_CONFLICT")

    def test_a_school_wide_classs_week_may_not_span_two_branches(self):
        """Two branch admins can each start building the same class's grid."""
        first = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.pry4a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_a1.pk,
        })
        self.assertEqual(first.status_code, 201, first.data)

        second = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.pry4a.pk, "day_of_week": 2,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_c1.pk,
        })
        self.assertEqual(second.status_code, 422, second.data)
        self.assertEqual(
            second.data["error"]["code"], "TIMETABLE_SPANS_BRANCHES",
        )
        # The message names where the class already is, because that is the
        # fact the person needs.
        self.assertIn("Lekki Branch", second.data["message"])


class StatusTests(_Base):
    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")

    def _fill(self, school_class=None):
        return self.post(self.admin, "calendar-slot-list", {
            "school_class": (school_class or self.jss1a).pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_a1.pk,
        })

    def test_a_class_that_has_never_been_touched_is_not_started(self):
        """An absent row, not a DRAFT default: three states, and this is one."""
        response = self.get(
            self.admin, "calendar-class-grid", class_id=self.jss1a.pk,
        )
        self.assertIsNone(response.data["data"]["status"])
        self.assertEqual(response.data["data"]["status_label"], "Not started")

    def test_the_first_lesson_makes_it_a_draft(self):
        self._fill()
        response = self.get(
            self.admin, "calendar-class-grid", class_id=self.jss1a.pk,
        )
        self.assertEqual(response.data["data"]["status"], PublishState.DRAFT)

    def test_editing_a_published_grid_returns_it_to_draft(self):
        """What was approved has changed, so it has to be published again."""
        self._fill()
        self.post(self.admin, "calendar-class-publish", class_id=self.jss1a.pk)
        self.assertEqual(
            ClassTimetable.all_objects.get(school_class=self.jss1a).status,
            PublishState.PUBLISHED,
        )

        self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 2,
            "period": self.p1.pk, "subject": self.physics.pk,
            "teacher": self.eze.pk, "room": self.room_a1.pk,
        })
        self.assertEqual(
            ClassTimetable.all_objects.get(school_class=self.jss1a).status,
            PublishState.DRAFT,
        )

    def test_the_grid_reports_gaps_without_judging_them(self):
        self._fill()
        response = self.get(
            self.admin, "calendar-class-grid", class_id=self.jss1a.pk,
        )
        data = response.data["data"]
        self.assertEqual(data["filled"], 1)
        self.assertGreater(data["lesson_periods"], 1)
        # Nothing knows how many periods a subject should get a week.
        for key in ("percent", "progress", "complete", "is_complete"):
            self.assertNotIn(key, data)

    def test_the_picker_shows_every_class_with_its_state(self):
        self._fill()
        response = self.get(self.admin, "calendar-class-list")
        rows = {row["name"]: row for row in response.data["data"]}
        self.assertEqual(rows["JSS1 A"]["lesson_count"], 1)
        self.assertEqual(rows["JSS1 A"]["status_label"], "Draft")
        self.assertEqual(rows["JSS1 B"]["status_label"], "Not started")


class DuplicateTests(_Base):
    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")
        for day in (1, 2):
            TimetableSlot.all_objects.create(
                tenant=self.tenant, session=self.year, school_class=self.jss1a,
                day_of_week=day, period=self.p1, subject=self.maths,
                teacher=self.eze, room=self.room_a1,
            )

    def test_preview_writes_nothing(self):
        response = self.post(
            self.admin, "calendar-class-duplicate",
            {"source_class": self.jss1a.pk},
            params={"preview": "1"}, class_id=self.jss1b.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["copied"], 2)
        self.assertEqual(
            TimetableSlot.all_objects.filter(school_class=self.jss1b).count(), 0,
        )

    def test_copying_replaces_the_target_grid(self):
        TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.jss1b,
            day_of_week=3, period=self.p2, subject=self.physics,
            teacher=self.eze, room=self.room_a2,
        )
        response = self.post(
            self.admin, "calendar-class-duplicate",
            {"source_class": self.jss1a.pk}, class_id=self.jss1b.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        rows = TimetableSlot.all_objects.filter(school_class=self.jss1b)
        # Replaced, not merged: a half-copied grid is harder to reason about.
        self.assertEqual(rows.count(), 2)
        self.assertFalse(rows.filter(day_of_week=3).exists())

    def test_copying_without_teachers_leaves_the_slots_unstaffed(self):
        self.post(
            self.admin, "calendar-class-duplicate",
            {"source_class": self.jss1a.pk, "keep_teachers": False},
            class_id=self.jss1b.pk,
        )
        rows = TimetableSlot.all_objects.filter(school_class=self.jss1b)
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(r.teacher_id is None for r in rows))

    def test_an_unstaffed_copy_saves_and_then_fails_to_publish(self):
        """The only path that can produce a gap, and why the gate checks for one."""
        self.post(
            self.admin, "calendar-class-duplicate",
            {"source_class": self.jss1a.pk, "keep_teachers": False},
            class_id=self.jss1b.pk,
        )
        response = self.post(
            self.admin, "calendar-class-publish", class_id=self.jss1b.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "TIMETABLE_INCOMPLETE")
        # Each gap is named, not counted: the person has to go and fill them.
        self.assertTrue(response.data["error"]["detail"]["items"])

    def test_a_copy_marks_the_target_a_draft(self):
        self.post(self.admin, "calendar-class-publish", class_id=self.jss1a.pk)
        self.post(
            self.admin, "calendar-class-duplicate",
            {"source_class": self.jss1a.pk}, class_id=self.jss1b.pk,
        )
        self.assertEqual(
            ClassTimetable.all_objects.get(school_class=self.jss1b).status,
            PublishState.DRAFT,
        )

    def test_a_class_cannot_be_copied_into_itself(self):
        response = self.post(
            self.admin, "calendar-class-duplicate",
            {"source_class": self.jss1a.pk}, class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 400, response.data)


class ClearTests(_Base):
    def test_clearing_needs_the_manage_key(self):
        from vs_rbac.models import TenantRolePermission

        TenantRolePermission.objects.filter(
            role=self.role, permission__key="academics.timetable.manage",
        ).update(granted=False)
        response = self.post(
            self.admin, "calendar-class-clear", class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_clearing_removes_only_that_classs_lessons(self):
        eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")
        for cls in (self.jss1a, self.jss1b):
            TimetableSlot.all_objects.create(
                tenant=self.tenant, session=self.year, school_class=cls,
                day_of_week=1, period=self.p1, subject=self.maths, teacher=eze,
            )
        self.post(self.admin, "calendar-class-clear", class_id=self.jss1a.pk)
        self.assertEqual(
            TimetableSlot.all_objects.filter(school_class=self.jss1a).count(), 0,
        )
        self.assertEqual(
            TimetableSlot.all_objects.filter(school_class=self.jss1b).count(), 1,
        )


class TeacherGridTests(_Base):
    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")

    def test_a_teacher_teaching_at_two_branches_gets_both_in_one_grid(self):
        TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.jss1a,
            day_of_week=1, period=self.p1, subject=self.physics,
            teacher=self.eze, room=self.room_a1,
        )
        TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.sss2,
            day_of_week=4, period=self.p1, subject=self.physics,
            teacher=self.eze, room=self.room_c1,
        )
        response = self.get(
            self.admin, "calendar-teacher-grid", user_id=self.eze.pk,
        )
        self.assertEqual(response.data["data"]["summary"]["teaching_periods"], 2)
        self.assertEqual(
            set(response.data["data"]["summary"]["branches"]),
            {"Lekki Branch", "Ikeja Branch"},
        )

    def test_the_grid_carries_no_email_address(self):
        response = self.get(
            self.admin, "calendar-teacher-grid", user_id=self.eze.pk,
        )
        self.assertNotIn("eze@brightfield.test", str(response.data))

    def test_the_workload_figure_carries_no_judgement(self):
        response = self.get(
            self.admin, "calendar-teacher-grid", user_id=self.eze.pk,
        )
        summary = response.data["data"]["summary"]
        self.assertEqual(
            set(summary) - {"branches"},
            {"teaching_periods", "free_periods", "busiest_day"},
        )

    def test_the_grid_is_read_only(self):
        response = self.client_for(self.admin).post(
            f"/v1/academics/timetable/teachers/{self.eze.pk}/"
            f"?tenant={self.tenant.slug}", {}, format="json",
        )
        self.assertEqual(response.status_code, 405, response.data)

    def test_another_tenants_user_answers_404(self):
        from vs_rbac.tests.helpers import make_school_admin

        stranger = make_school_admin(
            None, email="them@sunrise.test", tenant=self.other.tenant,
        )
        response = self.get(
            self.admin, "calendar-teacher-grid", user_id=stranger.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)
