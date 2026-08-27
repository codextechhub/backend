"""Rooms over HTTP. Security first, then the two rules that are this screen's."""
from __future__ import annotations

from vs_rbac.tests.helpers import make_school_admin

from ..models import Room, RoomType, TimetableSlot
from .base import _Base, _SingleBranchBase


class RoomSecurityTests(_Base):
    def test_a_caller_without_the_key_is_refused(self):
        stranger = make_school_admin(
            None, email="nobody@brightfield.test", tenant=self.tenant,
        )
        response = self.get(stranger, "calendar-room-list")
        self.assertEqual(response.status_code, 403, response.data)

    def test_another_tenants_room_answers_404_not_403(self):
        theirs = Room.all_objects.create(
            tenant=self.other.tenant, branch=self.other_branch,
            name="Their Room", room_type=RoomType.CLASSROOM,
        )
        response = self.get(self.admin, "calendar-room-detail", pk=theirs.pk)
        # 404, so a room id cannot be probed to learn another school's shape.
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_branch_admin_sees_only_their_branchs_rooms(self):
        response = self.get(self.ikeja_admin, "calendar-room-list")
        self.assertEqual(response.status_code, 200, response.data)
        names = {row["name"] for row in response.data["data"]}
        self.assertEqual(names, {"Block C Room 1"})


class RoomRuleTests(_Base):
    def test_the_same_name_at_two_branches_is_allowed(self):
        response = self.post(self.admin, "calendar-room-list", {
            "name": "Block A Room 1", "room_type": "CLASSROOM",
            "branch": self.ikeja.pk,
        })
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_repeat_within_one_branch_is_refused(self):
        response = self.post(self.admin, "calendar-room-list", {
            "name": "block a room 1", "room_type": "CLASSROOM",
            "branch": self.lekki.pk,
        })
        self.assertIn(response.status_code, (400, 409, 422), response.data)

    def test_the_name_refusal_names_the_field_the_branch_and_the_rule(self):
        """Not just a 4xx.

        This asserted only the status for a long time, and passed while the
        refusal was the platform's generic "A record with these details already
        exists" - which names no field, no row and no branch, and lands on a
        drawer holding a Name box AND a Code box. The person could not tell
        which of the two was wrong.

        The message must also state the rule CORRECTLY: a room name repeats
        freely across branches. Borrowing the catalogue's sentence would say
        names are unique across the school, which is false and would send a
        school hunting for a clash that is not there.
        """
        response = self.post(self.admin, "calendar-room-list", {
            "name": "block a room 1", "room_type": "CLASSROOM",
            "branch": self.lekki.pk,
        })
        self.assertEqual(response.data["error"]["code"], "DUPLICATE_NAME")
        self.assertEqual(response.data["error"]["detail"]["field"], "name")
        self.assertIn(self.lekki.name, response.data["message"])
        self.assertIn("within a branch", response.data["message"])

    def test_the_code_refusal_names_the_room_it_belongs_to(self):
        """A code is unique across the SCHOOL, and the message says so.

        The opposite scope from the name, on the same form, which is why the
        two refusals are written separately rather than sharing a sentence.
        """
        Room.all_objects.create(
            tenant=self.tenant, branch=self.lekki,
            name="Somewhere Else", code="ZZZ", room_type=RoomType.CLASSROOM,
        )
        response = self.post(self.admin, "calendar-room-list", {
            "name": "A New Room", "code": "zzz", "room_type": "CLASSROOM",
            "branch": self.ikeja.pk,
        })
        self.assertEqual(response.data["error"]["code"], "DUPLICATE_CODE")
        self.assertEqual(response.data["error"]["detail"]["field"], "code")
        self.assertIn("Somewhere Else", response.data["message"])
        self.assertIn("whole school", response.data["message"])

    def test_renaming_a_room_to_its_own_name_is_not_a_duplicate(self):
        """The row being edited must be excluded from its own check.

        Otherwise every save of an unchanged name is refused, which makes the
        drawer impossible to use for anything else on the form.
        """
        room = Room.all_objects.filter(
            tenant=self.tenant, branch=self.lekki,
        ).first()
        response = self.patch(
            self.admin, "calendar-room-detail",
            {"name": room.name, "capacity": 31}, pk=room.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_room_holding_lessons_cannot_be_deleted(self):
        TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.jss1a,
            day_of_week=1, period=self.p1, subject=self.maths, room=self.room_a1,
        )
        response = self.delete(self.admin, "calendar-room-detail", pk=self.room_a1.pk)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "PROTECTED_REFERENCE")
        # The message has to name the way out, because it renders verbatim
        # under a Delete button.
        self.assertIn("Deactivate it instead", response.data["message"])

    def test_an_unused_room_can_be_deleted(self):
        response = self.delete(self.admin, "calendar-room-detail", pk=self.room_a2.pk)
        self.assertEqual(response.status_code, 200, response.data)

    def test_usage_counts_lessons_and_papers(self):
        TimetableSlot.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.jss1a,
            day_of_week=1, period=self.p1, subject=self.maths, room=self.room_a1,
        )
        response = self.get(self.admin, "calendar-room-detail", pk=self.room_a1.pk)
        self.assertEqual(response.data["data"]["usage"]["label"], "1 lesson")

    def test_an_empty_room_says_so_rather_than_showing_a_zero(self):
        response = self.get(self.admin, "calendar-room-detail", pk=self.room_a2.pk)
        self.assertEqual(
            response.data["data"]["usage"]["label"], "Nothing scheduled here yet",
        )

    def test_capacity_is_never_compared_with_anything(self):
        """A class of forty fits a room of one, as far as the server is concerned."""
        small = Room.all_objects.create(
            tenant=self.tenant, branch=self.lekki, name="Cupboard",
            room_type=RoomType.OTHER, capacity=1,
        )
        response = self.post(self.admin, "calendar-slot-list", {
            "school_class": self.jss1a.pk, "day_of_week": 1,
            "period": self.p1.pk, "subject": self.maths.pk, "room": small.pk,
        })
        self.assertEqual(response.status_code, 201, response.data)


class RoomBranchTests(_Base):
    def test_a_room_cannot_be_school_wide(self):
        response = self.post(self.admin, "calendar-room-list", {
            "name": "Everywhere", "room_type": "HALL", "branch": None,
        })
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_branch_bound_caller_gets_their_own_branch_filled_in(self):
        response = self.post(self.ikeja_admin, "calendar-room-list", {
            "name": "New Ikeja Room", "room_type": "CLASSROOM",
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Room.all_objects.get(name="New Ikeja Room").branch_id, self.ikeja.pk,
        )

    def test_a_branch_bound_caller_cannot_put_a_room_at_another_branch(self):
        response = self.post(self.ikeja_admin, "calendar-room-list", {
            "name": "Sneaky", "room_type": "CLASSROOM", "branch": self.lekki.pk,
        })
        self.assertIn(response.status_code, (400, 403), response.data)


class SingleBranchRoomTests(_SingleBranchBase):
    def test_the_branch_field_is_absent_from_the_response(self):
        """Absent, not blank and not disabled: one option is noise."""
        self.post(self.admin, "calendar-room-list", {
            "name": "Room 1", "room_type": "CLASSROOM",
        })
        response = self.get(self.admin, "calendar-room-list")
        row = response.data["data"][0]
        self.assertNotIn("branch", row)
        self.assertNotIn("branch_name", row)

    def test_a_room_still_lands_on_the_only_branch(self):
        """The screen never asks, so nothing is sent - and it still has to land."""
        response = self.post(self.admin, "calendar-room-list", {
            "name": "Room 2", "room_type": "CLASSROOM",
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Room.all_objects.get(name="Room 2").branch_id, self.branch.pk,
        )
