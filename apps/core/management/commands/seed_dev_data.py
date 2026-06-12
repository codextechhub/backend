"""
seed_dev_data — one command that fills a fresh dev database with a connected
world covering every module EXCEPT finance/procurement/payments.

What it creates (idempotent — safe to re-run):
  1. Codex organogram: Division → Departments → Team, positions with
     reports_to, the 25 seeded vision staff in seats, staff profiles.
     (Powers the organogram APIs and vs_todo's assign-down rules.)
  2. Three ACTIVE schools with branches, contact info, branding rows,
     package setup (PackagePlan from seed_package), primary admins.
  3. School users per school: school admin, branch admins, teachers,
     students, parents — all ACTIVE with login passwords.
  4. RBAC: per-school role templates (Administrator / Branch Administrator /
     Teacher) with real permission grants, assigned to the users.
  5. One PENDING role-change request per school (fills approval queues).
  6. In-app notifications for school users.
  7. vs_todo tasks flowing down the Codex organogram.
  8. A few central audit events so the audit UI has data.

Prerequisites (run first, in this order):
  seed_actions, seed_all_permissions, seed_import_permissions,
  seed_workflow_permissions, seed_xvs_modules, seed_package,
  seed_prebuilt_role_templates, create_superuser --force,
  seed_vision_staff, seed_import, seed_notification_event_types,
  seed_notification_templates.
Afterwards run: seed_notification_settings --all

Passwords: vision staff "Vision@2025" (from seed_vision_staff);
           school users "School@2025".
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

SCHOOL_USER_PASSWORD = "School@2025"

SCHOOLS = [
    {
        "name": "Greenfield Academy",
        "slug": "greenfield-academy",
        "code": "GFA",
        "branches": [("Main Campus", "GFA-MAIN", True), ("Lekki Annex", "GFA-LEKKI", False)],
    },
    {
        "name": "Royal Crest College",
        "slug": "royal-crest-college",
        "code": "RCC",
        "branches": [("Main Campus", "RCC-MAIN", True), ("Ikeja Campus", "RCC-IKEJA", False)],
    },
    {
        "name": "Unity Heights School",
        "slug": "unity-heights-school",
        "code": "UHS",
        "branches": [("Main Campus", "UHS-MAIN", True)],
    },
]


class Command(BaseCommand):
    help = "Seed a connected dev dataset across all modules except finance/procurement/payments."

    @transaction.atomic
    def handle(self, *args, **options):
        self.now = timezone.now()
        self._organogram()
        schools = self._schools()
        users_by_school = self._school_users(schools)
        self._rbac(schools, users_by_school)
        self._notifications(schools, users_by_school)
        self._todo_tasks()
        self._audit_events(schools)
        self.stdout.write(self.style.SUCCESS(
            "\nDone. Now run:  manage.py seed_notification_settings --all\n"
            f"School-user password: {SCHOOL_USER_PASSWORD}"
        ))

    # ------------------------------------------------------------------ #
    # 1. Codex organogram                                                #
    # ------------------------------------------------------------------ #
    def _organogram(self):
        from vs_user.models import (
            OrgNode, PlatformStaffProfile, Position, PositionAssignment, User,
        )

        self.stdout.write(self.style.MIGRATE_HEADING("Organogram (Codex internal)..."))
        staff = list(
            User.objects.filter(user_type="CX_STAFF", email__endswith="@vision.edu")
            .order_by("email")
        )
        if not staff:
            self.stdout.write(self.style.ERROR("  No vision staff — run seed_vision_staff first."))
            return

        ops, _ = OrgNode.objects.get_or_create(
            code="CX-OPS",
            defaults=dict(name="Operations", kind=OrgNode.Kind.DIVISION),
        )
        eng, _ = OrgNode.objects.get_or_create(
            code="CX-ENG",
            defaults=dict(name="Engineering", kind=OrgNode.Kind.DEPARTMENT, parent=ops),
        )
        cs, _ = OrgNode.objects.get_or_create(
            code="CX-CS",
            defaults=dict(name="Customer Success", kind=OrgNode.Kind.DEPARTMENT, parent=ops),
        )
        platform_team, _ = OrgNode.objects.get_or_create(
            code="CX-ENG-PLAT",
            defaults=dict(name="Platform Team", kind=OrgNode.Kind.TEAM, parent=eng),
        )

        def seat(code, title, node, reports_to=None):
            pos, _ = Position.objects.get_or_create(
                code=code,
                defaults=dict(title=title, org_node=node, reports_to=reports_to),
            )
            return pos

        md = seat("CX-MD", "Managing Director", ops)
        eng_lead = seat("CX-ENG-LEAD", "Engineering Lead", eng, md)
        cs_lead = seat("CX-CS-LEAD", "Customer Success Lead", cs, md)
        engineers = [seat(f"CX-ENG-{i}", f"Software Engineer {i}", platform_team, eng_lead) for i in (1, 2, 3, 4)]
        cs_officers = [seat(f"CX-CS-{i}", f"Success Officer {i}", cs, cs_lead) for i in (1, 2, 3)]

        for node, pos in ((ops, md), (eng, eng_lead), (cs, cs_lead)):
            if node.head_position_id is None:
                node.head_position = pos
                node.save(update_fields=["head_position"])

        seats = [md, eng_lead, cs_lead, *engineers, *cs_officers]
        for user, pos in zip(staff, seats):
            PositionAssignment.objects.get_or_create(
                user=user, position=pos,
                defaults=dict(is_primary=True, start_date=self.now.date()),
            )
            PlatformStaffProfile.objects.get_or_create(
                user=user,
                defaults=dict(employee_id=f"CX{user.pk:04d}", job_title=pos.title),
            )
        self.stdout.write(f"  4 org nodes, {len(seats)} positions, {min(len(staff), len(seats))} filled seats.")

    # ------------------------------------------------------------------ #
    # 2. Schools, branches, package, primary admins                      #
    # ------------------------------------------------------------------ #
    def _schools(self):
        from vs_schools.models import (
            Branch, ContactInfo, PackagePlan, School, SchoolBranding,
            SchoolPackageSetup, SchoolPrimaryAdmin, SchoolStatus,
        )

        self.stdout.write(self.style.MIGRATE_HEADING("Schools and branches..."))
        plan = PackagePlan.objects.order_by("-max_students").first()
        made = []
        for spec in SCHOOLS:
            school, _ = School.objects.get_or_create(
                slug=spec["slug"],
                defaults=dict(
                    name=spec["name"],
                    code=spec["code"],
                    status=SchoolStatus.ACTIVE,
                    activated_at=self.now,
                    address=f"{spec['name']} Road, Lagos",
                    website=f"https://{spec['slug']}.example.com",
                    motto="Knowledge and Light",
                ),
            )
            for bname, btag, is_main in spec["branches"]:
                # Branch.code is an auto-allocated integer (per school) — key
                # on the name and let save() assign the code.
                Branch.all_objects.get_or_create(
                    school=school, name=bname,
                    defaults=dict(
                        is_main=is_main, status="ACTIVE",
                        country="Nigeria", state="Lagos",
                        email=f"{btag.lower()}@{spec['slug']}.example.com",
                        activated_at=self.now,
                        _type="Secondary",
                    ),
                )
            SchoolBranding.objects.get_or_create(school=school)
            if plan:
                setup, _ = SchoolPackageSetup.objects.get_or_create(
                    school=school,
                    defaults=dict(
                        package_plan=plan,
                        student_capacity=1000, teacher_capacity=100, admin_capacity=10,
                        subscription_expires_at=self.now + timedelta(days=365),
                    ),
                )
                # Enable every module except the finance stack (user scope).
                from vs_schools.models import XVSModules
                modules = XVSModules.objects.exclude(
                    key__in=["finance", "procurement", "payments", "vendors"]
                )
                setup.enabled_modules.set(modules)
            contact, _ = ContactInfo.objects.get_or_create(
                email=f"admin@{spec['slug']}.example.com",
                defaults=dict(full_name=f"{spec['name']} Administrator", phone="+2348000000000"),
            )
            SchoolPrimaryAdmin.objects.get_or_create(school=school, defaults=dict(contact=contact))
            made.append(school)
        self.stdout.write(f"  {len(made)} schools ready.")
        return made

    # ------------------------------------------------------------------ #
    # 3. School users                                                    #
    # ------------------------------------------------------------------ #
    def _school_users(self, schools):
        from vs_schools.models import Branch
        from vs_user.models import User

        self.stdout.write(self.style.MIGRATE_HEADING("School users..."))
        first_names = ["Adaeze", "Bunmi", "Chuka", "Dami", "Efe", "Folake", "Gozie", "Hauwa",
                       "Ikenna", "Jumoke", "Kelechi", "Lola"]
        result = {}
        total = 0
        for school in schools:
            branches = list(Branch.all_objects.filter(school=school).order_by("-is_main"))
            main = branches[0]
            domain = f"{school.slug}.example.com"

            def mk(email, first, last, user_type, branch):
                user = User.objects.filter(email=email).first()
                if user is None:
                    user = User.objects.create_user(
                        email=email, password=SCHOOL_USER_PASSWORD,
                        first_name=first, last_name=last,
                        user_type=user_type, status="ACTIVE",
                        school=school, branch=branch,
                    )
                return user

            users = {"admins": [], "branch_admins": [], "staff": [], "students": [], "parents": []}
            users["admins"].append(mk(f"admin@{domain}", "Amaka", school.code.title(), "SCHOOL_ADMIN", main))
            for i, br in enumerate(branches):
                users["branch_admins"].append(
                    mk(f"branch{i + 1}.admin@{domain}", first_names[i], "Balogun", "BRANCH_ADMIN", br)
                )
            for i in range(3):
                users["staff"].append(
                    mk(f"teacher{i + 1}@{domain}", first_names[i + 2], "Teacher", "STAFF",
                       branches[i % len(branches)])
                )
            for i in range(4):
                users["students"].append(
                    mk(f"student{i + 1}@{domain}", first_names[i + 5], "Student", "STUDENT",
                       branches[i % len(branches)])
                )
            for i in range(2):
                users["parents"].append(
                    mk(f"parent{i + 1}@{domain}", first_names[i + 9], "Parent", "PARENT", main)
                )
            result[school.pk] = users
            total += sum(len(v) for v in users.values())
        self.stdout.write(f"  {total} school users (password: {SCHOOL_USER_PASSWORD}).")
        return result

    # ------------------------------------------------------------------ #
    # 4. RBAC roles + assignments + a pending change request             #
    # ------------------------------------------------------------------ #
    def _rbac(self, schools, users_by_school):
        from vs_rbac.models import (
            Permission, SchoolRoleChangeRequest, SchoolRolePermission,
            SchoolRoleTemplate, SchoolUserRoleAssignment,
        )

        self.stdout.write(self.style.MIGRATE_HEADING("RBAC roles and assignments..."))
        perm_keys = list(
            Permission.objects.filter(is_active=True)
            .order_by("key").values_list("key", flat=True)
        )

        def grants(prefixes, limit=12):
            keys = [k for k in perm_keys if any(k.startswith(p) for p in prefixes)]
            return keys[:limit]

        role_specs = [
            ("School Administrator", ["schools.", "identity.", "rbac.", "notify.", "import."], "admins"),
            ("Branch Administrator", ["schools.branch", "identity.", "notify."], "branch_admins"),
            ("Teacher", ["notify.", "system."], "staff"),
        ]
        assignments = 0
        for school in schools:
            users = users_by_school[school.pk]
            for role_name, prefixes, bucket in role_specs:
                role = SchoolRoleTemplate.all_objects.filter(
                    school=school, name__iexact=role_name
                ).first()
                if role is None:
                    role = SchoolRoleTemplate.objects.create(school=school, name=role_name)
                    for key in grants(prefixes):
                        SchoolRolePermission.objects.get_or_create(
                            role=role, permission_id=key, defaults=dict(granted=True),
                        )
                for user in users[bucket]:
                    exists = SchoolUserRoleAssignment.all_objects.filter(
                        school=school, user=user, role=role, assignment_status="ACTIVE",
                    ).exists()
                    if not exists:
                        SchoolUserRoleAssignment.objects.create(school=school, user=user, role=role)
                        assignments += 1

            teacher_role = SchoolRoleTemplate.all_objects.get(school=school, name="Teacher")
            SchoolRoleChangeRequest.objects.get_or_create(
                school=school, target_role=teacher_role,
                requested_by=users["admins"][0],
                status="PENDING",
                defaults=dict(
                    justification="Teachers need import access for class lists.",
                    submitted_at=self.now,
                ),
            )
        self.stdout.write(f"  3 roles per school, {assignments} new assignments, 1 pending change request each.")

    # ------------------------------------------------------------------ #
    # 5. Notifications                                                   #
    # ------------------------------------------------------------------ #
    def _notifications(self, schools, users_by_school):
        from vs_notifications.models import Notification, NotificationEventType

        self.stdout.write(self.style.MIGRATE_HEADING("Notifications..."))
        event = (
            NotificationEventType.objects.filter(key="import.completed").first()
            or NotificationEventType.objects.first()
        )
        if event is None:
            self.stdout.write(self.style.WARNING("  No event types — run seed_notification_event_types."))
            return
        channel_field = Notification._meta.get_field("channel")
        in_app = next(
            (c for c, _ in channel_field.choices if "APP" in str(c).upper()),
            channel_field.choices[0][0],
        )
        status_field = Notification._meta.get_field("status")
        sent = next(
            (c for c, _ in status_field.choices if str(c).upper() in ("SENT", "DELIVERED")),
            status_field.choices[0][0],
        )
        count = 0
        for school in schools:
            users = users_by_school[school.pk]
            for user in users["admins"] + users["staff"][:1]:
                _, created = Notification.objects.get_or_create(
                    school=school, recipient=user, event_type=event, channel=in_app,
                    subject="Welcome to XVision",
                    defaults=dict(
                        body="Your dev environment is seeded and ready.",
                        status=sent,
                    ),
                )
                count += int(created)
        self.stdout.write(f"  {count} in-app notifications.")

    # ------------------------------------------------------------------ #
    # 6. vs_todo tasks down the organogram                               #
    # ------------------------------------------------------------------ #
    def _todo_tasks(self):
        from vs_todo.models import Task
        from vs_user.models import PositionAssignment

        self.stdout.write(self.style.MIGRATE_HEADING("ToDo tasks..."))
        seats = {
            pa.position.code: pa.user
            for pa in PositionAssignment.objects.select_related("user", "position")
        }
        md, eng_lead = seats.get("CX-MD"), seats.get("CX-ENG-LEAD")
        cs_lead, eng1 = seats.get("CX-CS-LEAD"), seats.get("CX-ENG-1")
        specs = [
            (md, eng_lead, "Ship the academic core MVP", "HIGH", 21),
            (md, cs_lead, "Prepare the pilot-school onboarding pack", "MEDIUM", 14),
            (eng_lead, eng1, "Profile slow dashboard endpoints", "MEDIUM", 7),
            (eng_lead, eng1, "Add OpenAPI schema generation", "LOW", 30),
        ]
        count = 0
        for assigner, assignee, title, priority, days in specs:
            if assigner is None or assignee is None:
                continue
            _, created = Task.objects.get_or_create(
                assignee=assignee, title=title,
                defaults=dict(
                    assigned_by=assigner,
                    assigned_by_name=assigner.full_name,
                    description="Seeded dev task.",
                    priority=priority,
                    deadline=(self.now + timedelta(days=days)).date(),
                ),
            )
            count += int(created)
        self.stdout.write(f"  {count} tasks.")

    # ------------------------------------------------------------------ #
    # 7. Audit events                                                    #
    # ------------------------------------------------------------------ #
    def _audit_events(self, schools):
        from vs_audit.services import emit_audit_event

        self.stdout.write(self.style.MIGRATE_HEADING("Audit events..."))
        for school in schools:
            emit_audit_event(
                module_key="SCHOOLS",
                action_type="CREATE",
                entity_type="School",
                entity_id=str(school.pk),
                entity_label=school.name,
                summary=f"Seeded school {school.name}.",
            )
        self.stdout.write(f"  {len(schools)} events emitted (best-effort).")
