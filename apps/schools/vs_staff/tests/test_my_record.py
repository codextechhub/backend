"""Every member of staff can find their own record without the directory.

A teacher holds no directory key, and their own record is where they apply for
leave and correct their phone number, so the app asks the server which record
is theirs and links to it.
"""
from __future__ import annotations

from .base import StaffFixture


class MyRecordTests(StaffFixture):
    def test_a_teacher_with_no_directory_key_finds_their_own_record(self):
        response = self.get(self.eze.user, "staff-mine")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"], {"id": self.eze.pk})

    def test_an_account_with_no_staff_record_is_told_so(self):
        response = self.get(self.admin, "staff-mine")
        self.assertEqual(response.status_code, 404, response.content)

    def test_a_record_at_another_school_is_not_theirs_here(self):
        # Sunrise's teacher asks at Sunrise and finds their Sunrise record only.
        response = self.get(self.solo_staff.user, "staff-mine")
        self.assertEqual(response.data["data"], {"id": self.solo_staff.pk})
