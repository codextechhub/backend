"""Clash detection, and the two properties of it that are easy to reverse.

The first is that the query spans the tenant. Narrowing it by
``visible_branch_ids`` looks like tightening security and is the bug this whole
module exists to prevent: a person cannot be at two branches at once, so a
narrowed query offers a booking it cannot check.

The second is the disclosure rule. A clash is always reported; the detail is
what varies. A caller who cannot see the other branch is told the day, the
period and that the person is already teaching elsewhere - and never the other
class, the other room, the other slot id or any branch id.
"""
from __future__ import annotations

from django.db import connection

from ..models import PublishState, TimetableSlot
from .base import _Base


class ConstraintShapeTests(_Base):
    def test_there_is_no_unique_constraint_over_teacher_or_room(self):
        """The constraint a reviewer will ask for, and which would break the product.

        A clash has to be STORABLE to be shown: a school that discovers at
        Period 5 that Mrs Adeyemi is double-booked needs to save the grid, see
        both cells in red, and resolve it when the head of department is back.
        Adding either constraint should fail here rather than change the product
        silently.
        """
        constraints = {
            c.name for c in TimetableSlot._meta.constraints
        }
        self.assertEqual(constraints, {"uq_slot_class_day_period"})

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT indexdef FROM pg_indexes
                WHERE tablename = %s AND indexdef ILIKE '%%UNIQUE%%'
                """,
                [TimetableSlot._meta.db_table],
            )
            defs = [row[0].lower() for row in cursor.fetchall()]
        for definition in defs:
            if "teacher_id" in definition and "school_class_id" not in definition:
                self.fail(f"A unique index over teacher exists: {definition}")
            if "room_id" in definition and "school_class_id" not in definition:
                self.fail(f"A unique index over room exists: {definition}")


class TeacherClashTests(_Base):
    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")

    _DEFAULT = object()

    def _slot(self, school_class, room, day=1, period=None, teacher=_DEFAULT):
        return TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=school_class,
            day_of_week=day, period=period or self.p1, subject=self.maths,
            teacher=self.eze if teacher is self._DEFAULT else teacher, room=room,
        )

    def test_both_slots_persist_and_the_second_warns(self):
        self._slot(self.jss1a, self.room_a1)
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1b.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_a2.pk,
        })
        # The write stands. A clash never refuses.
        self.assertEqual(response.status_code, 201, response.data)
        codes = {w["code"] for w in response.data["data"]["warnings"]}
        self.assertIn("TEACHER_DOUBLE_BOOKED", codes)
        self.assertEqual(TimetableSlot.all_objects.count(), 2)

    def test_a_different_period_warns_about_nothing(self):
        self._slot(self.jss1a, self.room_a1, period=self.p1)
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1b.pk, "day_of_week": 1,
            "period": self.p2.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_a2.pk,
        })
        self.assertEqual(response.data["data"]["warnings"], [])

    def test_a_different_day_warns_about_nothing(self):
        self._slot(self.jss1a, self.room_a1, day=1)
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1b.pk, "day_of_week": 2,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": self.eze.pk, "room": self.room_a2.pk,
        })
        self.assertEqual(response.data["data"]["warnings"], [])

    def test_a_room_booked_twice_warns(self):
        other = self.make_teacher("funke@brightfield.test", "Funke", "Adeyemi")
        self._slot(self.jss1a, self.room_a1)
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1b.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk,
            "teacher": other.pk, "room": self.room_a1.pk,
        })
        codes = {w["code"] for w in response.data["data"]["warnings"]}
        self.assertIn("ROOM_DOUBLE_BOOKED", codes)


class CrossBranchClashTests(_Base):
    """Mr Eze teaches at Lekki and at Ikeja, which is ordinary and is the case
    the whole redaction rule exists for."""

    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")
        # His Lekki lesson: Monday, Period 1.
        self.lekki_slot = TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.jss1a,
            day_of_week=1, period=self.p1, subject=self.physics,
            teacher=self.eze, room=self.room_a1,
        )

    def test_the_clash_query_is_not_narrowed_by_branch(self):
        """The test that fails if somebody adds visible_branch_ids to it."""
        response = self.post(self.ikeja_admin, "calendar-slot-list", {
            "school_class": self.sss2.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.physics.pk,
            "teacher": self.eze.pk, "room": self.room_c1.pk,
        })
        self.assertEqual(response.status_code, 201, response.data)
        codes = {w["code"] for w in response.data["data"]["warnings"]}
        self.assertIn(
            "TEACHER_DOUBLE_BOOKED", codes,
            "The Ikeja admin was told nothing about a Lekki booking - the "
            "clash query has been narrowed by branch.",
        )

    def test_the_warning_names_neither_the_other_class_nor_its_room(self):
        response = self.post(self.ikeja_admin, "calendar-slot-list", {
            "school_class": self.sss2.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.physics.pk,
            "teacher": self.eze.pk, "room": self.room_c1.pk,
        })
        warnings = response.data["data"]["warnings"]
        blob = str(warnings)
        self.assertIn("another branch", blob)
        self.assertNotIn("JSS1 A", blob)
        self.assertNotIn("Block A Room 1", blob)
        self.assertNotIn("Lekki", blob)
        # An id is enumerable, so the other slot's is withheld with its name.
        for warning in warnings:
            self.assertNotIn(self.lekki_slot.pk, warning["slot_ids"])

    def test_a_school_level_caller_is_told_both_sides_in_full(self):
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.sss2.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.physics.pk,
            "teacher": self.eze.pk, "room": self.room_c1.pk,
        })
        blob = str(response.data["data"]["warnings"])
        self.assertIn("JSS1 A", blob)


class PublishGateTests(_Base):
    def setUp(self):
        self.eze = self.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")

    _DEFAULT = object()

    def _fill(self, school_class, room, teacher=_DEFAULT, period=None):
        """`teacher=None` means a genuine gap, not "use the default"."""
        return TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=school_class,
            day_of_week=1, period=period or self.p1, subject=self.maths,
            teacher=self.eze if teacher is self._DEFAULT else teacher, room=room,
        )

    def test_a_clean_grid_publishes(self):
        self._fill(self.jss1a, self.room_a1)
        response = self.post(
            self.admin, "calendar-class-publish", class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], PublishState.PUBLISHED)

    def test_a_grid_with_a_clash_is_refused(self):
        self._fill(self.jss1a, self.room_a1)
        self._fill(self.jss1b, self.room_a2)  # same teacher, same slot
        response = self.post(
            self.admin, "calendar-class-publish", class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "TIMETABLE_HAS_CLASHES")

    def test_a_grid_with_a_gap_is_refused_before_clashes_are_mentioned(self):
        """Incompleteness first: it is the more actionable message."""
        self._fill(self.jss1a, self.room_a1, teacher=None)
        self._fill(self.jss1b, self.room_a2)
        response = self.post(
            self.admin, "calendar-class-publish", class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "TIMETABLE_INCOMPLETE")

    def test_a_cross_branch_clash_blocks_a_branch_admin_who_cannot_see_it(self):
        """Blocked by a clash whose details they are never shown. Deliberate."""
        self._fill(self.jss1a, self.room_a1)                     # Lekki
        self._fill(self.sss2, self.room_c1)                      # Ikeja, same teacher
        response = self.post(
            self.ikeja_admin, "calendar-class-publish", class_id=self.sss2.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)
        blob = str(response.data)
        self.assertNotIn("JSS1 A", blob)
        self.assertNotIn("Block A Room 1", blob)

    def test_publishing_needs_its_own_key(self):
        from vs_rbac.models import TenantRolePermission

        self._fill(self.jss1a, self.room_a1)
        TenantRolePermission.objects.filter(
            role=self.role, permission__key="academics.timetable.publish",
        ).update(granted=False)
        response = self.post(
            self.admin, "calendar-class-publish", class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_the_gate_recomputes_rather_than_reading_a_flag(self):
        """A clash created behind the API must still block."""
        self._fill(self.jss1a, self.room_a1)
        response = self.post(
            self.admin, "calendar-class-publish", class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

        # Straight into the ORM, past every write path.
        self._fill(self.jss1b, self.room_a2)
        response = self.post(
            self.admin, "calendar-class-publish", class_id=self.jss1a.pk,
        )
        self.assertEqual(response.status_code, 409, response.data)
