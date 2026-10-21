"""
Tests for the `seed_school_permissions` management command (WP-B1 / A.2).

Covers:
  (a) running the command creates the school + academics permission keys;
  (b) prebuilt roles carry the expected default counts;
  (c) pre-existing tenant role templates (natively provisioned by prebuilt key)
      gain the granted rows on re-run (backfill);
  (d) an explicit deny is not overwritten by the backfill;
  (e) get_effective_permissions for a user with an active school_admin
      assignment returns `school.students.view`.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from vs_rbac.evaluator import get_effective_permissions
from vs_rbac.models import (
    Permission,
    PermissionResource,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from schools.vs_schools.models import School
from vs_user.models import User


def _seed_actions_and_roles():
    """Prerequisite seeds the school-permission command relies on."""
    call_command("seed_actions", stdout=StringIO(), stderr=StringIO())
    call_command("seed_prebuilt_role_templates", stdout=StringIO(), stderr=StringIO())


def _run_school_seed(**kwargs):
    out = StringIO()
    call_command("seed_school_permissions", stdout=out, stderr=StringIO(), **kwargs)
    return out.getvalue()


class SeedSchoolPermissionsKeyTests(TestCase):
    """What one run of the seed writes, read back by each test.

    The seed runs once for the class, against a database holding only the
    actions and prebuilt roles it depends on, so every key read here was
    created by that run. Tests that re-run it do so themselves.
    """

    @classmethod
    def setUpTestData(cls):
        _seed_actions_and_roles()
        _run_school_seed()

    def test_creates_school_and_academics_keys(self):
        # A representative spread across both modules and several verbs.
        for key in (
            "school.dashboard.view",
            "school.students.view",
            "school.administrators.suspend",
            "school.administrators.reactivate",
            "school.roles.assign",
            "school.roles.create",
            "school.roles.update",
            "school.roles.approve",
            "school.roles.delete",
            "academics.session.view",
            "academics.calendar.delete",
            "academics.classes.assign",
            "academics.structure.view",
            "academics.structure.archive",
            "academics.subject.create",
            "academics.subject.archive",
        ):
            self.assertTrue(
                Permission.objects.filter(key=key).exists(),
                f"Expected permission key {key} to be created.",
            )

    def test_the_gradebook_module_registers_its_nineteen_keys(self):
        keys = set(
            Permission.objects.filter(module_id="gradebook").values_list("key", flat=True)
        )
        self.assertEqual(keys, {
            "gradebook.hub.view", "gradebook.sheet.view", "gradebook.sheet.enter",
            "gradebook.sheet.enter_any", "gradebook.sheet.remind",
            "gradebook.moderation.approve", "gradebook.class.submit",
            "gradebook.class.publish", "gradebook.hold.view", "gradebook.hold.update",
            "gradebook.release.view", "gradebook.result.view", "gradebook.setup.view",
            "gradebook.setup.update", "gradebook.amendment.create",
            "gradebook.amendment.approve", "gradebook.sheet.import",
            "gradebook.analytics.view", "gradebook.analytics.export",
        })

    def test_total_key_count(self):
        """The school and academics modules register exactly 101 keys.

        Deliberately a hand-maintained number: the school permission surface
        growing is something a person should have to notice and agree to, so
        adding a key is meant to fail here until someone updates it, and the
        table in ``seed_school_permissions`` is where the reason for each key
        is written.

        Some shapes in that table look accidental and are not:

        * ``academics.structure`` covers departments, programs and levels, and
          ``academics.subject`` is its own resource because a branch admin may
          create a subject and may not create a programme.
        * ``academics.timetable`` and ``academics.exam`` are resources of their
          own rather than more uses of the calendar keys, because adding a
          public holiday, rebuilding a timetable and scheduling exams are sold
          and granted separately.
        * The staff register keys stay on ``school.teachers``: renaming a key
          school-fe checks by name would leave live keys governing nothing, so
          the resource DESCRIPTION says "Staff records" instead.
          ``school.staff`` exists for the spreadsheet import alone.
        * ``school.field_access`` carries view and update for Field Access.
        * ``school.organogram`` is the school's own org chart, a resource of
          its own because every member of staff reads it while the register's
          dated history stays with the administrators.
        * ``school.students.change_branch`` moves a pupil to another branch
          with their fee account. It is not ``school.students.transfer``,
          which means leaving for another school.
        * ``school.portal_access.issue`` gives a pupil their own sign-in
          account, and ``school.notices`` and ``school.class_notices`` are the
          school end of the pupil portal: an administrator posts to any
          audience in reach, a teacher to the classes they teach.
        * ``school.messages``, ``school.family_messages``,
          ``school.message_groups``, ``school.message_broadcasts`` and
          ``school.message_welfare`` are messaging: taking part is for current
          staff, and the three ``inspect`` keys read a group, a broadcast or a
          pupil's conversation without joining it, with every opening audited.
        * There is no key for a child's medical details. Blood group, allergies
          and conditions are registered fields of ``school.students``, so who
          reads and corrects them is a switch on the role, set on the Field
          Access screen and not by ticking a permission.
        """
        self.assertEqual(
            Permission.objects.filter(module_id__in=["school", "academics"]).count(),
            111,
        )

    def test_field_access_view_is_open_and_update_is_restricted(self):
        # Viewing switches opens nothing; changing one opens a field at once.
        view = Permission.objects.get(key="school.field_access.view")
        update = Permission.objects.get(key="school.field_access.update")
        for perm in (view, update):
            self.assertEqual(perm.sensitivity_level, "CRITICAL", perm.key)
            self.assertEqual(perm.scope, "TENANT", perm.key)
        self.assertFalse(view.is_restricted)
        self.assertTrue(update.is_restricted)

    def test_impersonation_keys_are_critical_and_restricted(self):
        for key in (
            "school.impersonation.start",
            "school.impersonation.end",
            "school.impersonation.view",
        ):
            perm = Permission.objects.get(key=key)
            self.assertEqual(perm.sensitivity_level, "CRITICAL", key)
            self.assertTrue(perm.is_restricted, key)

    def test_override_keys_are_critical_and_restricted(self):
        # `.view` is as restricted as the write keys: without it a user must not be
        # able to learn that permission exceptions exist on their account.
        for key in (
            "school.user_overrides.view",
            "school.user_overrides.create",
            "school.user_overrides.delete",
        ):
            perm = Permission.objects.get(key=key)
            self.assertEqual(perm.sensitivity_level, "CRITICAL", key)
            self.assertTrue(perm.is_restricted, key)

    def test_sensitivity_levels_applied(self):
        self.assertEqual(
            Permission.objects.get(key="school.students.transition").sensitivity_level,
            "SENSITIVE",
        )
        self.assertEqual(
            Permission.objects.get(key="school.dashboard.view").sensitivity_level,
            "NORMAL",
        )

    def test_idempotent(self):
        before = Permission.objects.filter(module_id__in=["school", "academics"]).count()
        out = _run_school_seed()
        after = Permission.objects.filter(module_id__in=["school", "academics"]).count()
        self.assertEqual(before, after)
        self.assertIn("0 new permission(s) created", out)

    def test_a_fields_only_resource_is_registered_and_mints_no_key(self):
        """Guardians exist as a resource so field switches have a home.

        The guardian endpoints carry the student keys, so a guardian key would
        be a switch governing nothing. The resource is still needed, because
        ``sync_field_registry`` refuses a field declaration whose resource is
        missing.
        """
        resource = PermissionResource.objects.get(
            module_id="school", name="guardians",
        )
        self.assertEqual(resource.label, "Guardians")
        self.assertTrue(resource.description)
        self.assertFalse(
            Permission.objects.filter(resource=resource).exists(),
        )

    def test_the_staff_register_reads_as_staff_rather_than_teachers(self):
        """The resource holds the bursar and the registrar as much as the teacher.

        The key keeps its ``teachers`` slug because school-fe checks it by
        name; the readable name is what the screens show.
        """
        self.assertEqual(
            PermissionResource.objects.get(
                module_id="school", name="teachers",
            ).label,
            "Staff",
        )

    def test_a_blank_label_on_an_existing_resource_is_filled_in(self):
        """A resource created before labels existed gains one on the next run."""
        PermissionResource.objects.filter(
            module_id="school", name="teachers",
        ).update(label="")
        _run_school_seed()
        self.assertEqual(
            PermissionResource.objects.get(
                module_id="school", name="teachers",
            ).label,
            "Staff",
        )


class SeedSchoolPrebuiltDefaultsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _seed_actions_and_roles()
        _run_school_seed()

    def _defaults(self, role_key):
        role = PrebuiltRoleTemplate.objects.get(key=role_key)
        return set(
            PrebuiltRolePermission.objects
            .filter(prebuilt_role=role)
            .values_list("permission_id", flat=True)
        )

    def test_school_admin_gets_all_keys(self):
        """A school admin holds every school and academics key (111), every
        attendance key but ``attendance.register.mark`` (10), which is the class
        teacher's own-class key and is superseded for an administrator by
        ``attendance.register.mark_any``, and fourteen gradebook keys: everything
        but entering scores (the main teacher's), moderating, submitting a class
        and the fee-hold update key (the finance admin's), and
        ``gradebook.sheet.enter_any``, which no role holds by default.
        """
        keys = self._defaults("school_admin")
        self.assertEqual(len(keys), 135)
        self.assertEqual(
            {k for k in keys if k.startswith("gradebook.")},
            {"gradebook.hub.view", "gradebook.sheet.view", "gradebook.sheet.remind",
             "gradebook.class.publish", "gradebook.hold.view", "gradebook.release.view",
             "gradebook.result.view", "gradebook.setup.view", "gradebook.setup.update",
             "gradebook.amendment.create", "gradebook.amendment.approve",
             "gradebook.sheet.import", "gradebook.analytics.view",
             "gradebook.analytics.export"},
        )
        self.assertNotIn("attendance.register.mark", keys)
        self.assertIn("attendance.register.mark_any", keys)
        self.assertIn("school.field_access.update", self._defaults("school_admin"))

    def test_only_school_admin_gets_field_access_by_default(self):
        field_access = {"school.field_access.view", "school.field_access.update"}
        self.assertTrue(field_access <= self._defaults("school_admin"))
        self.assertFalse(field_access & self._defaults("branch_admin"))
        self.assertFalse(field_access & self._defaults("teacher"))

    def test_only_school_admin_gets_impersonation_by_default(self):
        # The most powerful school keys must never be a branch_admin/teacher
        # default - they are opt-in for anyone below the school admin.
        impersonation = {
            "school.impersonation.start",
            "school.impersonation.end",
            "school.impersonation.view",
        }
        self.assertTrue(impersonation <= self._defaults("school_admin"))
        self.assertFalse(impersonation & self._defaults("branch_admin"))
        self.assertFalse(impersonation & self._defaults("teacher"))

    def test_only_school_admin_gets_permission_overrides_by_default(self):
        overrides = {
            "school.user_overrides.view",
            "school.user_overrides.create",
            "school.user_overrides.delete",
        }
        self.assertTrue(overrides <= self._defaults("school_admin"))
        self.assertFalse(overrides & self._defaults("branch_admin"))
        self.assertFalse(overrides & self._defaults("teacher"))

    def test_branch_admin_default_count(self):
        """55 permissions cover branch-level school operations.

        A branch admin draws the org chart and appoints people to it, and the
        views keep those writes to their own branch's units and posts.

        A branch admin reads and corrects a child's blood group, allergies and
        conditions exactly where the school turns those switches on for their
        role, so there is no key left to default.

        A branch admin exports their own branch's roll, and the dataset is
        narrowed to the branches they can see, so the file can never be wider
        than the screen they started from. school.students.import is withheld:
        a bad import is the fastest way to damage a school's records and is
        reversible only through the engine's rollback, so it stays with the
        school admin.

        A branch admin builds and publishes their own branch's grid -
        academics.timetable.view, .create, .update and .publish - so that it
        does not wait on the head office. The delete permission is withheld:
        a branch adds and edits its own entries, and removing them is the
        school's call.

        A branch admin reads the school profile because the currency and term
        structure govern screens they work in. Changing it stays with the
        school admin.

        A branch admin reads the structure and works with subjects because a
        subject may belong to one branch. Creating or retiring a department,
        programme, or level is a statement about the whole school's curriculum,
        so those actions stay with the school admin.

        A branch admin may import staff because they may already add staff one
        at a time. They set exam schedules and maintain staff records, but do
        not delete an exam, promote the roll, or change employment status.

        A branch admin holds school.students.change_branch, and moves a pupil
        only between branches they both work at: one pinned to Ikeja holds the
        key and moves nobody.
        """
        branch_admin = self._defaults("branch_admin")
        # 64 school and academics keys (pupil portal access, reading and
        # posting notices, and the six messaging keys among them), plus the nine attendance keys a branch
        # administrator holds (everything but settings and the own-class mark),
        # plus nine gradebook keys: the hub, reading and reminding on sheets,
        # the release list, a student's results, reading assessment setup,
        # asking for a correction to a published result, and the analytics and
        # their export.
        self.assertEqual(len(branch_admin), 82)
        self.assertEqual(
            {k for k in branch_admin if k.startswith("gradebook.")},
            {"gradebook.hub.view", "gradebook.sheet.view", "gradebook.sheet.remind",
             "gradebook.release.view", "gradebook.result.view", "gradebook.setup.view",
             "gradebook.amendment.create", "gradebook.analytics.view",
             "gradebook.analytics.export"},
        )
        self.assertNotIn("gradebook.amendment.approve", branch_admin)
        self.assertIn("attendance.register.mark_any", branch_admin)
        self.assertIn("attendance.correction.approve", branch_admin)
        self.assertNotIn("attendance.settings.update", branch_admin)
        self.assertNotIn("attendance.register.mark", branch_admin)
        self.assertIn("school.organogram.assign", branch_admin)
        self.assertIn("school.staff.import", branch_admin)
        self.assertNotIn("school.students.import", branch_admin)
        self.assertIn("school.teachers.assign", branch_admin)
        self.assertIn("school.leave.update", branch_admin)
        self.assertIn("school.leave.cancel", branch_admin)
        self.assertNotIn("school.teachers.transition", branch_admin)
        self.assertIn("school.students.export", branch_admin)
        self.assertNotIn("school.students.import", branch_admin)
        self.assertIn("academics.timetable.publish", branch_admin)
        self.assertNotIn("academics.timetable.delete", branch_admin)
        self.assertIn("academics.subject.create", self._defaults("branch_admin"))
        self.assertNotIn("academics.structure.create", self._defaults("branch_admin"))
        self.assertIn("school.profile.view", self._defaults("branch_admin"))
        self.assertNotIn("school.profile.update", self._defaults("branch_admin"))

    def test_teacher_default_count(self):
        """14 = 12, plus academics.electives.view and .update: a teacher reads
        who takes which elective in any class they can open, and saves it for
        the class they lead. The save is judged per class by the view, so the
        key opens no class a teacher does not lead.

        12 = 13, less school.teachers.view: a teacher reads colleagues'
        profiles as far as the school's staff profile policy shows colleagues
        and line managers, and their own through the same policy. Holding the
        directory key read every colleague at their branch in full.

        13 = 12, plus school.organogram.view: every member of staff reads
        the school's org chart, and draws none of it.

        12 = 11, plus academics.exam.view: a teacher reads the exam
        schedule they are invigilating without being able to change it.

        11 = 10, plus the one key M12 gives a teacher: school.leave.apply.

        The 10 was 9, plus the one read key M14 gives a teacher.

        A teacher holds academics.timetable.view because reading their own
        timetable is the single most useful thing this platform will ever do
        for them. They write none of it: a slot is changed on the class grid by
        somebody who holds .update.

        The 9 below was 7, plus the two read keys M13 gives a teacher.

        A teacher reads the structure and the subjects and writes neither.
        Note that academics.classes.update was already a teacher default before
        M13, which the FRD records as an open question rather than a decision
        this module took.
        """
        keys = self._defaults("teacher")
        # 19 school and academics keys (reading notices, posting them to
        # the classes they teach, and the three messaging keys among them), plus the three attendance keys a teacher
        # holds: reading a register, taking their own class's, and asking for
        # a correction. Taking any class, reminding and deciding are the
        # administrators'. Plus five gradebook keys: reading sheets, entering
        # the scores of the subjects they lead, uploading them from a
        # spreadsheet, moderating as a head of department and submitting the
        # class they lead; which sheet, subject or class is the service's rule,
        # not the key's.
        self.assertEqual(len(keys), 27)
        self.assertEqual(
            {k for k in keys if k.startswith("gradebook.")},
            {"gradebook.sheet.view", "gradebook.sheet.enter", "gradebook.sheet.import",
             "gradebook.moderation.approve", "gradebook.class.submit"},
        )
        self.assertEqual(
            {k for k in keys if k.startswith("attendance.")},
            {"attendance.register.view", "attendance.register.mark",
             "attendance.correction.create"},
        )
        self.assertNotIn("school.teachers.view", keys)
        self.assertIn("school.organogram.view", keys)
        self.assertNotIn("school.organogram.create", keys)
        # M12 gives a teacher exactly one key: applying for their own leave.
        # Reading a colleague's is not something every colleague may do, so
        # school.leave.view stops at the two admin roles.
        self.assertIn("school.leave.apply", keys)
        self.assertNotIn("school.leave.view", keys)
        self.assertNotIn("school.teachers.assign", keys)
        self.assertIn("academics.timetable.view", keys)
        self.assertNotIn("academics.timetable.update", keys)
        self.assertIn("academics.structure.view", keys)
        self.assertIn("academics.subject.view", keys)
        self.assertNotIn("academics.structure.create", keys)
        self.assertNotIn("academics.subject.create", keys)
        self.assertIn("school.dashboard.view", keys)
        self.assertIn("school.students.view", keys)
        self.assertIn("academics.classes.update", keys)
        # Teacher must NOT get create/manage verbs.
        self.assertNotIn("school.students.create", keys)
        self.assertNotIn("school.branches.view", keys)


class SeedSchoolBackfillTests(TestCase):
    """The critical step - pre-existing tenant role templates get grants.

    Roles are found by the backfill through their native prebuilt key
    (key=<prebuilt.key> or key=<prebuilt.key>-<branch pk>).
    """

    @classmethod
    def setUpTestData(cls):
        _seed_actions_and_roles()
        cls.school = School.objects.create(
            name="Backfill Academy", slug="backfill", status="ACTIVE"
        )
        cls.prebuilt = PrebuiltRoleTemplate.objects.get(key="school_admin")
        # Native lineage: a system role provisioned straight into the tenant
        # tables with the prebuilt key.
        cls.role = TenantRoleTemplate.objects.create(
            tenant=cls.school.tenant,
            key="school_admin",
            name="School Admin",
            is_system_role=True,
        )

    def setUp(self):
        # No permissions attached to it yet.
        self.assertEqual(
            TenantRolePermission.objects.filter(role=self.role).count(), 0
        )

    def test_backfill_grants_rows_on_run(self):
        _run_school_seed()
        keys = set(
            TenantRolePermission.objects
            .filter(role=self.role, granted=True)
            .values_list("permission_id", flat=True)
        )
        # school_admin defaults are every school and academics key, every
        # attendance key but the class teacher's own-class mark, and fourteen
        # gradebook keys.
        self.assertEqual(len(keys), 135)
        self.assertIn("attendance.register.mark_any", keys)
        self.assertIn("school.students.view", keys)
        self.assertIn("school.roles.create", keys)
        self.assertIn("school.roles.approve", keys)
        self.assertIn("academics.classes.assign", keys)
        # The backfill is what gives ALREADY-provisioned schools the new
        # impersonation keys - provision_role_from_prebuilt only copies
        # prebuilt permissions on fresh role creation.
        self.assertIn("school.impersonation.start", keys)
        self.assertIn("school.impersonation.end", keys)
        self.assertIn("school.impersonation.view", keys)

    def test_backfill_is_idempotent(self):
        _run_school_seed()
        first = TenantRolePermission.objects.filter(role=self.role).count()
        _run_school_seed()
        second = TenantRolePermission.objects.filter(role=self.role).count()
        self.assertEqual(first, second)

    def test_explicit_deny_not_overwritten(self):
        # Admin explicitly denied a permission that is a school_admin default.
        _run_school_seed()  # first run registers the Permission rows
        TenantRolePermission.objects.filter(
            role=self.role, permission_id="school.students.view"
        ).update(granted=False)
        _run_school_seed()
        row = TenantRolePermission.objects.get(
            role=self.role, permission_id="school.students.view"
        )
        self.assertFalse(row.granted, "Backfill must never flip an explicit deny.")
        # Other defaults still granted.
        self.assertTrue(
            TenantRolePermission.objects.filter(
                role=self.role, permission_id="school.teachers.view", granted=True
            ).exists()
        )

    def test_native_teacher_role_backfilled_with_teacher_defaults_only(self):
        # Native lineage: provisioned straight into the tenant tables with the
        # prebuilt key (no legacy row at all).
        teacher_role = TenantRoleTemplate.objects.create(
            tenant=self.school.tenant,
            key="teacher",
            name="Teacher",
            is_system_role=True,
        )
        _run_school_seed()
        keys = set(
            TenantRolePermission.objects
            .filter(role=teacher_role, granted=True)
            .values_list("permission_id", flat=True)
        )
        self.assertEqual(len(keys), 27)
        self.assertNotIn("school.teachers.view", keys)
        self.assertIn("school.students.view", keys)
        self.assertNotIn("school.students.create", keys)
        self.assertIn("attendance.register.mark", keys)

    def test_non_system_role_with_prebuilt_like_key_not_backfilled(self):
        # A custom (non-system) role must not silently inherit prebuilt
        # defaults just because its key resembles a prebuilt key.
        custom = TenantRoleTemplate.objects.create(
            tenant=self.school.tenant,
            key="teacher-lead",
            name="Lead Teacher (custom)",
            is_system_role=False,
        )
        _run_school_seed()
        self.assertEqual(
            TenantRolePermission.objects.filter(role=custom).count(), 0
        )


class SchoolDefaultsReachCopiesOnceTests(TestCase):
    """A default reaches each school's roles once, and a school's removal stands.

    Bright Star runs two branches (Lekki and Ikeja) and Sunrise runs one. Each
    has a School Admin, a Branch Admin per branch and a Teacher per branch,
    provisioned from the library the way a new school's roles are. The seed
    offers a default to those copies at the moment the library role gains it
    (``vs_rbac.library_growth``) and never again, so a key a school took off
    one of its roles is not put back by the next deploy.
    """

    NEW_KEY = "school.students.change_branch"

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.services import provision_role_from_prebuilt
        from vs_rbac.tests.helpers import make_branch, make_school

        _seed_actions_and_roles()
        _run_school_seed()

        bright = make_school(slug="grow-bright", name="Bright Star").tenant
        lekki = make_branch(bright, name="Lekki", is_main=True)
        ikeja = make_branch(bright, name="Ikeja", is_main=False)
        sunrise = make_school(slug="grow-sunrise", name="Sunrise").tenant
        sunrise_main = make_branch(sunrise, name="Sunrise Main", is_main=True)

        def provision(tenant, key, branch=None):
            return provision_role_from_prebuilt(
                tenant=tenant, branch=branch, prebuilt_key=key,
            )

        cls.bright_admin = provision(bright, "school_admin")
        cls.lekki_admin = provision(bright, "branch_admin", lekki)
        cls.ikeja_admin = provision(bright, "branch_admin", ikeja)
        cls.lekki_teacher = provision(bright, "teacher", lekki)
        cls.sunrise_admin = provision(sunrise, "school_admin")
        cls.sunrise_branch_admin = provision(sunrise, "branch_admin", sunrise_main)
        cls.sunrise_teacher = provision(sunrise, "teacher", sunrise_main)
        cls.bright_finance = provision(bright, "finance_admin")
        cls.sunrise_finance = provision(sunrise, "finance_admin")
        cls.admin_copies = {
            cls.bright_admin.pk, cls.lekki_admin.pk, cls.ikeja_admin.pk,
            cls.sunrise_admin.pk, cls.sunrise_branch_admin.pk,
        }

    @staticmethod
    def holds(role, key):
        return TenantRolePermission.objects.filter(
            role=role, permission_id=key, granted=True,
        ).exists()

    def holders(self, key):
        return set(
            TenantRolePermission.objects.filter(permission_id=key, granted=True)
            .values_list("role_id", flat=True)
        )

    def take_off(self, role, key):
        TenantRolePermission.objects.filter(role=role, permission_id=key).delete()

    def test_a_key_a_school_took_off_stays_off(self):
        """Bright Star stops its School Admin exporting the roll, and Ikeja's
        Branch Admin cancelling leave. Lekki's Branch Admin keeps cancelling."""
        self.take_off(self.bright_admin, "school.students.export")
        self.take_off(self.ikeja_admin, "school.leave.cancel")
        self.take_off(self.sunrise_teacher, "school.leave.apply")

        _run_school_seed()

        self.assertFalse(self.holds(self.bright_admin, "school.students.export"))
        self.assertFalse(self.holds(self.ikeja_admin, "school.leave.cancel"))
        self.assertFalse(self.holds(self.sunrise_teacher, "school.leave.apply"))
        self.assertTrue(self.holds(self.lekki_admin, "school.leave.cancel"))
        self.assertTrue(self.holds(self.sunrise_admin, "school.students.export"))

    def test_a_new_default_reaches_every_admin_copy_once(self):
        """Both schools opened before moving a pupil between branches shipped.

        The key reaches every School Admin and Branch Admin copy at both
        schools on the next run, and no Teacher. Sunrise then takes it off its
        School Admin, and the run after that leaves it off.
        """
        PrebuiltRolePermission.objects.filter(permission_id=self.NEW_KEY).delete()
        TenantRolePermission.objects.filter(permission_id=self.NEW_KEY).delete()

        _run_school_seed()

        self.assertEqual(self.holders(self.NEW_KEY), self.admin_copies)
        self.assertFalse(self.holds(self.lekki_teacher, self.NEW_KEY))

        self.take_off(self.sunrise_admin, self.NEW_KEY)
        _run_school_seed()

        self.assertEqual(
            self.holders(self.NEW_KEY), self.admin_copies - {self.sunrise_admin.pk},
        )

    def test_the_fee_hold_keys_reach_every_finance_admin_copy_and_nobody_else_below_admin(self):
        """Finance Admin owns finance and payments by prefix, and the gradebook's
        fee-hold keys by this seed. Both schools opened before holds shipped; on
        the next run each school's Finance Admin can see holds and place them,
        each School Admin can see them, and no Branch Admin or Teacher can.
        """
        keys = ("gradebook.hold.view", "gradebook.hold.update")
        PrebuiltRolePermission.objects.filter(permission_id__in=keys).delete()
        TenantRolePermission.objects.filter(permission_id__in=keys).delete()

        _run_school_seed()

        finance = {self.bright_finance.pk, self.sunrise_finance.pk}
        self.assertEqual(self.holders("gradebook.hold.update"), finance)
        self.assertEqual(
            self.holders("gradebook.hold.view"),
            finance | {self.bright_admin.pk, self.sunrise_admin.pk},
        )
        for role in (self.lekki_admin, self.lekki_teacher, self.sunrise_branch_admin):
            self.assertFalse(self.holds(role, "gradebook.hold.view"))

    def test_the_billing_keys_reach_every_finance_admin_copy(self):
        """Billing a class reads its list, the term it is billed for and the
        school's fee rules. Both schools opened before Finance Admin held those
        keys; on the next run each school's Finance Admin can read the roll,
        the classes and the sessions, and read and change the fee rules.
        """
        keys = (
            "school.students.view", "academics.classes.view",
            "academics.session.view", "school.fees.view", "school.fees.update",
        )
        finance = (self.bright_finance, self.sunrise_finance)
        PrebuiltRolePermission.objects.filter(
            prebuilt_role__key="finance_admin", permission_id__in=keys,
        ).delete()
        TenantRolePermission.objects.filter(
            role__in=finance, permission_id__in=keys,
        ).delete()

        _run_school_seed()

        for role in finance:
            for key in keys:
                self.assertTrue(self.holds(role, key), f"{role.name} lacks {key}")

    def test_finance_admin_reads_no_medical_field_by_default(self):
        """Reading the roll does not open a child's medical record: blood group,
        allergies and conditions are sensitive, so no role reads them until a
        school turns the switch on.
        """
        from vs_rbac.field_registry import get_declaration

        specs = {s.name: s for s in get_declaration("school", "students").fields}
        for name in ("blood_group", "allergies", "conditions", "status_reason"):
            spec = specs[name]
            self.assertTrue(spec.sensitive, name)
            self.assertNotIn("finance_admin", spec.read_by, name)

    def test_nobody_holds_the_enter_any_key_by_default(self):
        _run_school_seed()
        self.assertEqual(self.holders("gradebook.sheet.enter_any"), set())
        self.assertFalse(
            PrebuiltRolePermission.objects.filter(
                permission_id="gradebook.sheet.enter_any",
            ).exists()
        )

    def test_a_second_run_adds_nothing(self):
        _run_school_seed()
        before = TenantRolePermission.objects.count()
        _run_school_seed()
        self.assertEqual(TenantRolePermission.objects.count(), before)


class SchoolAdminEffectivePermissionsTests(TestCase):
    """End-to-end: a user with an active school_admin assignment resolves grants."""

    @classmethod
    def setUpTestData(cls):
        _seed_actions_and_roles()
        cls.school = School.objects.create(
            name="Effective High", slug="effective", status="ACTIVE"
        )
        cls.role = TenantRoleTemplate.objects.create(
            tenant=cls.school.tenant,
            key="school_admin",
            name="School Admin",
            is_system_role=True,
        )
        _run_school_seed()

        cls.user = User.objects.create_user(
            email="head@effective.test",
            password="Str0ng!pass123",
            status="ACTIVE",
            first_name="Head",
            last_name="Teacher",
            tenant=cls.school.tenant,
        )
        TenantUserRoleAssignment.objects.create(
            tenant=cls.school.tenant,
            user=cls.user,
            role=cls.role,
            assignment_status="ACTIVE",
        )

    def test_effective_permissions_include_students_view(self):
        perms = get_effective_permissions(self.user, tenant=self.school.tenant)
        self.assertIn("school.students.view", perms)
        self.assertIn("academics.classes.assign", perms)
        self.assertIn("school.roles.update", perms)
        self.assertIn("school.roles.approve", perms)

    def test_effective_permissions_respect_explicit_deny(self):
        TenantRolePermission.objects.filter(
            role=self.role, permission_id="school.students.view"
        ).update(granted=False)
        # Clear any request-scoped cache on the user instance.
        if hasattr(self.user, "_rbac_effective_perms"):
            delattr(self.user, "_rbac_effective_perms")
        perms = get_effective_permissions(self.user, tenant=self.school.tenant)
        self.assertNotIn("school.students.view", perms)
