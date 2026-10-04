"""Avatar employment flags come from staff records, not account security."""

from core.person_exit import exited_states, person_is_exited, prime_exit_states
from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.serializers import staff_holder
from vs_user.models import User

from .base import StaffFixture


class StaffAvatarExitTests(StaffFixture):
    def test_directory_and_detail_distinguish_exit_from_suspension(self):
        self.eze.employment_status = EmploymentStatus.SUSPENDED
        self.eze.save(update_fields=["employment_status"])
        self.registrar.employment_status = EmploymentStatus.RESIGNED
        self.registrar.save(update_fields=["employment_status"])
        self.ikeja_teacher.employment_status = EmploymentStatus.TERMINATED
        self.ikeja_teacher.save(update_fields=["employment_status"])

        directory = self.get(self.admin, "staff-list")
        self.assertEqual(directory.status_code, 200, directory.data)
        rows = {row["id"]: row for row in directory.data["data"]}
        self.assertFalse(rows[self.eze.pk]["is_exited"])
        self.assertTrue(rows[self.registrar.pk]["is_exited"])
        self.assertTrue(rows[self.ikeja_teacher.pk]["is_exited"])

        detail = self.get(self.admin, "staff-detail", pk=self.registrar.pk)
        self.assertTrue(detail.data["data"]["is_exited"])
        self.assertTrue(staff_holder(self.registrar, {"request": None})["is_exited"])

    def test_bulk_lookup_keeps_other_tenants_out(self):
        self.solo_staff.employment_status = EmploymentStatus.RESIGNED
        self.solo_staff.save(update_fields=["employment_status"])
        self.eze.user.status = User.Status.SUSPENDED
        self.eze.user.save(update_fields=["status"])

        states = exited_states(
            self.tenant, (self.eze.user_id, self.solo_staff.user_id),
        )
        self.assertEqual(states[self.eze.user_id], False)
        self.assertEqual(states[self.solo_staff.user_id], False)

    def test_mixed_school_and_platform_page_is_three_queries_then_cached(self):
        from vs_tenants.models import Tenant
        from vs_user.models import PlatformStaffProfile

        platform = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        operator = User.objects.create_user(
            tenant=platform, email="departed-avatar-operator@codex.test",
            first_name="Departed", last_name="Operator",
        )
        PlatformStaffProfile.objects.create(
            user=operator,
            employment_status=PlatformStaffProfile.EmploymentStatus.EXITED,
        )
        self.registrar.employment_status = EmploymentStatus.RESIGNED
        self.registrar.save(update_fields=["employment_status"])
        context = {}

        with self.assertNumQueries(3):
            states = prime_exit_states(context, (
                self.registrar.user_id, self.eze.user_id, operator.pk,
            ))

        self.assertEqual(states, {
            self.registrar.user_id: True,
            self.eze.user_id: False,
            operator.pk: True,
        })
        with self.assertNumQueries(0):
            self.assertTrue(person_is_exited(context, self.registrar.user_id))
            self.assertFalse(person_is_exited(context, self.eze.user_id))
            self.assertTrue(person_is_exited(context, operator.pk))
