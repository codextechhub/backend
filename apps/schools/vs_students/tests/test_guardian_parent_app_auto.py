"""Guardians given the parent app automatically, and the email that rule needs.

Brightfield has two branches and Sunrise one, and neither has touched the rule,
so both give the parent app automatically. Mrs Amina Yusuf is the guardian an
enrolment names. Mrs Ngozi Eze teaches at Brightfield and her son is enrolled
there, so she already has a sign-in. An invitation is sent only after the
change that prompted it commits, so each test runs the commit callbacks
itself.
"""
from __future__ import annotations

from django.db import transaction

from vs_audit.models import AuditActionType, AuditEvent
from vs_rbac.tests.helpers import make_assignment, make_role, make_school_admin
from vs_user.models import User, UserInvitation

from ..constants import StudentStatus
from ..models import Guardian
from ..services import guardian_portal_access as access
from ..services.guardians import link, update_guardian
from ..services.status import transition
from .test_guardian_settings import (
    EMAIL_REQUIRED,
    _GuardianRulesFixture,
    _execute_import_row,
)

AMINA = "amina.yusuf@example.ng"
TEACHER_EMAIL = "ngozi.eze@brightfield.test"


class _AutoFixture(_GuardianRulesFixture):

    def rules_body(self, **overrides):
        return super().rules_body(**{"parent_app_auto_access": True, **overrides})

    def committed(self, fn, *args, **kwargs):
        """Run *fn* and then the callbacks its commit would run."""
        with self.captureOnCommitCallbacks(execute=True):
            return fn(*args, **kwargs)

    def enrol(self, user=None, body=None):
        response = self.committed(
            self.post, user or self.admin, "student-list", body or self.enrolment_body(),
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def invited(self, guardian) -> bool:
        guardian.refresh_from_db()
        return (
            guardian.user_id is not None
            and guardian.user.status == User.Status.PENDING
            and UserInvitation.objects.filter(user=guardian.user, is_used=False).exists()
        )

    def amina(self, tenant=None):
        return Guardian.all_objects.get(tenant=tenant or self.tenant, email=AMINA)


# ── the setting ─────────────────────────────────────────────────────────────

class SettingTests(_AutoFixture):

    def test_a_school_that_has_set_nothing_gives_the_app_and_needs_an_email(self):
        for user in (self.admin, self.solo_admin):
            with self.subTest(school=user.tenant.slug):
                data = self.get(user, "student-guardian-rules").data["data"]
                self.assertTrue(data["parent_app_auto_access"])
                self.assertFalse(data["email_required"])
                self.assertTrue(data["binding_email_required"])

    def test_turning_it_off_restores_the_schools_own_email_choice(self):
        data = self.set_rules(parent_app_auto_access=False).data["data"]
        self.assertFalse(data["parent_app_auto_access"])
        self.assertFalse(data["binding_email_required"])
        data = self.set_rules(parent_app_auto_access=False, email_required=True).data["data"]
        self.assertTrue(data["binding_email_required"])

    def test_a_save_that_leaves_it_out_keeps_what_is_stored(self):
        self.set_rules(parent_app_auto_access=False)
        body = self.rules_body(min_per_student=2)
        body.pop("parent_app_auto_access")
        response = self.put(self.settings_admin, "student-guardian-rules", body)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["parent_app_auto_access"])
        self.assertEqual(response.data["data"]["min_per_student"], 2)

    def test_a_value_that_is_not_true_or_false_is_refused(self):
        response = self.put(
            self.settings_admin, "student-guardian-rules",
            self.rules_body(parent_app_auto_access="sometimes"),
        )
        self.assertIn("parent_app_auto_access", self.error_detail(response))

    def test_changing_it_needs_the_settings_key(self):
        refused = self.put(
            self.admin, "student-guardian-rules",
            self.rules_body(parent_app_auto_access=False),
        )
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertTrue(
            self.get(self.admin, "student-guardian-rules").data["data"]["parent_app_auto_access"],
        )

    def test_one_school_turning_it_off_leaves_the_other_on(self):
        self.set_solo_rules(parent_app_auto_access=False)
        self.assertTrue(
            self.get(self.admin, "student-guardian-rules").data["data"]["parent_app_auto_access"],
        )
        self.assertFalse(
            self.get(self.solo_admin, "student-guardian-rules").data["data"]["parent_app_auto_access"],
        )


# ── the email it needs ──────────────────────────────────────────────────────

class EmailRequiredTests(_AutoFixture):

    def test_enrolment_refuses_a_new_guardian_without_one_at_both_shapes_of_school(self):
        for user, body in (
            (self.admin, self.enrolment_body(guardians=[self.guardian_row(email="")])),
            (self.solo_admin, self.solo_enrolment_body(guardians=[self.guardian_row(email="")])),
        ):
            with self.subTest(school=user.tenant.slug):
                detail = self.error_detail(self.post(user, "student-list", body))
                self.assertEqual(detail, {"guardians": [{"email": [EMAIL_REQUIRED]}]})

    def test_with_the_rule_off_a_guardian_needs_no_email(self):
        self.set_rules(parent_app_auto_access=False)
        response = self.post(
            self.admin, "student-list",
            self.enrolment_body(guardians=[self.guardian_row(email="")]),
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_the_link_drawer_and_an_edit_are_held_to_it(self):
        child = self.student()
        detail = self.error_detail(self.post(
            self.admin, "student-guardians",
            {"full_name": "Mrs. Nkechi Eze", "phone": "08035550500", "relationship": "AUNT"},
            pk=child.pk,
        ))
        self.assertEqual(detail, {"email": [EMAIL_REQUIRED]})
        held = self.guardian(name="Mrs. Ada Nwosu", phone="08035550601", email="ada@example.ng")
        detail = self.error_detail(self.patch(
            self.admin, "guardian-detail", {"email": ""}, pk=held.pk,
        ))
        self.assertEqual(detail, {"email": [EMAIL_REQUIRED]})

    def test_the_student_import_refuses_a_blank_guardian_email(self):
        from .test_guardian_settings import _import_issues
        from .test_import_export import _ImportFixture

        issues = _import_issues(self, [_ImportFixture.row(self, **{"Guardian Email": ""})])
        self.assertIn(
            ("Guardian Email", EMAIL_REQUIRED),
            [(i["column_name"], i["message"]) for i in issues if i["severity"] == "error"],
        )


class EmailWaivedTests(_AutoFixture):
    """A caller who may not write a guardian's email is not held to the rule."""

    def test_a_caller_without_email_write_is_waived_and_the_invitation_waits(self):
        from vs_rbac.tests.helpers import (
            install_declared_fields,
            make_role_permission,
            set_field_access,
        )

        install_declared_fields("school.guardians")
        role = make_role(self.school, name="Front desk", key="front_desk")
        for key in ("school.students.view", "school.students.create", "school.students.update"):
            if key in self.permissions:
                make_role_permission(role, self.permissions[key])
        set_field_access(role, "school.guardians.email", read=False, write=False)
        desk = make_school_admin(None, email="desk@brightfield.test", tenant=self.tenant)
        make_assignment(self.school, desk, role, branch=None)

        data = self.get(desk, "student-guardian-rules").data["data"]
        self.assertTrue(data["parent_app_auto_access"])
        self.assertFalse(data["binding_email_required"])

        # Mr Okafor is added without an email; adding one later sends the invitation.
        child = self.student()
        okafor = self.guardian(name="Mr. Emeka Okafor", phone="08035550400", email="")
        self.committed(
            link, child, okafor, relationship="FATHER", is_primary=False, actor=self.admin,
        )
        self.assertFalse(self.invited(okafor))
        self.committed(update_guardian, okafor, actor=self.admin, email="emeka.okafor@example.ng")
        self.assertTrue(self.invited(okafor))


# ── the invitation ──────────────────────────────────────────────────────────

class InvitationTests(_AutoFixture):

    def test_enrolment_invites_the_guardian_at_both_shapes_of_school(self):
        for user, body, tenant in (
            (self.admin, self.enrolment_body(), self.tenant),
            (self.solo_admin, self.solo_enrolment_body(), self.solo.tenant),
        ):
            with self.subTest(school=tenant.slug):
                self.enrol(user, body)
                amina = self.amina(tenant)
                self.assertTrue(self.invited(amina))
                self.assertEqual(amina.user.tenant_id, tenant.pk)
                self.assertTrue(AuditEvent.objects.filter(
                    action_type=AuditActionType.STUDENT_PORTAL_ACCESS_ISSUED,
                    entity_type="Guardian", entity_id=str(amina.pk),
                ).exists())

    def test_with_the_rule_off_nobody_is_invited(self):
        self.set_rules(parent_app_auto_access=False)
        self.enrol()
        self.assertIsNone(self.amina().user_id)

    def test_nothing_is_sent_until_the_enrolment_commits(self):
        with self.captureOnCommitCallbacks(execute=False):
            self.post(self.admin, "student-list", self.enrolment_body())
        self.assertIsNone(self.amina().user_id)

    def test_a_rolled_back_link_invites_nobody(self):
        child = self.student()
        held = self.guardian(name="Mrs. Ada Nwosu", phone="08035550601", email="ada@example.ng")
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    link(child, held, relationship="MOTHER", is_primary=True, actor=self.admin)
                    raise RuntimeError("the enrolment is refused after the link")
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        held.refresh_from_db()
        self.assertIsNone(held.user_id)

    def test_an_applicant_is_invited_only_when_admitted(self):
        applicant = self.student(status=StudentStatus.APPLICANT, first="Tobi")
        held = self.guardian(name="Mrs. Ada Nwosu", phone="08035550601", email="ada@example.ng")
        self.committed(
            link, applicant, held, relationship="MOTHER", is_primary=True, actor=self.admin,
        )
        self.assertFalse(self.invited(held))
        self.committed(
            transition, applicant, StudentStatus.ENROLLED, actor=self.admin, reason="Admitted.",
        )
        self.assertTrue(self.invited(held))

    def test_linking_a_held_guardian_to_a_child_on_the_roll_invites_them(self):
        child = self.student(branch=self.ikeja)
        held = self.guardian(name="Mrs. Ada Nwosu", phone="08035550601", email="ada@example.ng")
        response = self.committed(
            self.post, self.admin, "student-guardians",
            {"guardian_id": held.pk, "relationship": "MOTHER"}, pk=child.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(self.invited(held))

    def test_an_invited_guardian_is_not_sent_a_second_invitation(self):
        self.enrol()
        amina = self.amina()
        first = UserInvitation.objects.get(user=amina.user).token_hash
        self.enrol(body=self.enrolment_body(first_name="Musa"))
        self.assertEqual(UserInvitation.objects.filter(user=amina.user).count(), 1)
        self.assertEqual(UserInvitation.objects.get(user=amina.user).token_hash, first)

    def test_a_guardian_whose_access_was_taken_away_is_not_invited_again(self):
        self.enrol()
        amina = self.amina()
        user = amina.user
        user.status = User.Status.ACTIVE
        user.is_active = True
        user.save()
        access.revoke(amina, actor=self.admin)
        self.assertEqual(access.state_of(Guardian.all_objects.get(pk=amina.pk)), access.REVOKED)

        self.enrol(body=self.enrolment_body(first_name="Musa"))
        self.assertEqual(access.state_of(Guardian.all_objects.get(pk=amina.pk)), access.REVOKED)

    def test_a_teacher_who_is_a_parent_keeps_her_one_account_and_gets_no_email(self):
        teacher = make_school_admin(
            None, email=TEACHER_EMAIL, tenant=self.tenant, first_name="Ngozi", last_name="Eze",
        )
        make_assignment(
            self.school, teacher, make_role(self.school, name="Teacher", key="teacher"),
            branch=self.lekki,
        )
        users_before = User.objects.count()
        self.enrol(body=self.enrolment_body(
            first_name="Amara", last_name="Eze",
            guardians=[self.guardian_row(
                full_name="Mrs. Ngozi Eze", phone="08041112233", email=TEACHER_EMAIL,
            )],
        ))
        guardian = Guardian.all_objects.get(tenant=self.tenant, email=TEACHER_EMAIL)
        self.assertEqual(guardian.user_id, teacher.pk)
        self.assertEqual(access.state_of(guardian), access.ACTIVE)
        self.assertEqual(User.objects.count(), users_before)
        self.assertFalse(UserInvitation.objects.filter(user=teacher).exists())

    def test_a_refusal_is_skipped_and_the_change_still_stands(self):
        """Mrs Ada Nwosu was given her son's own sign-in address, which no parent may use."""
        pupil_sign_in = make_school_admin(None, email="tobi.nwosu@example.ng", tenant=self.tenant)
        self.student(first="Tobi", number="BFS/7", user=pupil_sign_in)
        held = self.guardian(name="Mrs. Ada Nwosu", phone="08035550601", email="tobi.nwosu@example.ng")
        with self.assertLogs("schools.vs_students.services.parent_app_auto", "INFO"):
            row = self.committed(
                link, self.student(first="Kemi"), held, relationship="MOTHER",
                is_primary=True, actor=self.admin,
            )
        self.assertIsNotNone(row.pk)
        held.refresh_from_db()
        self.assertIsNone(held.user_id)

    def test_the_student_import_invites_the_guardian(self):
        from .test_import_export import _ImportFixture

        student = self.committed(_execute_import_row, self, _ImportFixture.row(self))
        guardian = student.guardian_links.get().guardian
        self.assertTrue(self.invited(guardian))

    def test_the_household_import_invites_the_guardian(self):
        from ..guardian_imports import build_links, resolve_file

        child = self.student(first="Ifeoma", last="Okeke", number="BFS/9")
        resolved = resolve_file([{
            "guardian_full_name": "Mrs. Uche Okeke", "guardian_phone": "08035550700",
            "guardian_email": "uche.okeke@example.ng", "student_number": "BFS/9",
            "relationship": "Mother", "is_primary": "Yes",
        }], tenant=self.tenant)
        self.assertFalse([i for _n, i in resolved.issues if i.severity == "error"])
        self.committed(build_links, resolved, tenant=self.tenant, actor=self.admin)
        self.assertTrue(self.invited(child.guardian_links.get().guardian))

    def test_a_child_off_the_roll_does_not_prompt_an_invitation(self):
        gone = self.student(status=StudentStatus.WITHDRAWN, first="Ola")
        held = self.guardian(name="Mrs. Ify Obi", phone="08035550604", email="ify.obi@example.ng")
        self.committed(link, gone, held, relationship="MOTHER", is_primary=True, actor=self.admin)
        held.refresh_from_db()
        self.assertIsNone(held.user_id)

    def test_a_development_seeder_invites_nobody(self):
        from schools.vs_schools.dev.targets import quiet

        child = self.student()
        held = self.guardian(name="Mrs. Ada Nwosu", phone="08035550601", email="ada@example.ng")
        with quiet():
            self.committed(link, child, held, relationship="MOTHER", is_primary=True, actor=self.admin)
        held.refresh_from_db()
        self.assertIsNone(held.user_id)
