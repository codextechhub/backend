"""
seed_dev_data — one command that fills a fresh dev database with a connected
world covering every module EXCEPT finance/procurement/payments.

FOCUS: CX (Codex) staff are the main subjects — the platform currently runs
as the Codex staff intranet. The seeder builds a complete 25-seat company
(5 departments, 3-level reporting lines, rich HR profiles, platform roles,
todo board, login/security history, impersonation sessions). The schools
exist as the CUSTOMER BASE those staff manage, not as the protagonists.

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

How to run:
dropdb cx_db && createdb cx_db
cd apps && ../cx/bin/python manage.py migrate --settings=apps.settings.local
# then the seed chain (order matters):
for c in seed_all_permissions seed_xvs_modules seed_package; do
../cx/bin/python manage.py $c --settings=apps.settings.local; done
../cx/bin/python manage.py create_superuser --force --settings=apps.settings.local
../cx/bin/python manage.py seed_vision_staff --settings=apps.settings.local
../cx/bin/python manage.py seed_import --settings=apps.settings.local
../cx/bin/python manage.py seed_notification_event_types --settings=apps.settings.local
../cx/bin/python manage.py seed_notification_templates --settings=apps.settings.local
../cx/bin/python manage.py seed_dev_data --settings=apps.settings.local       # ← the new command
../cx/bin/python manage.py seed_notification_settings --all --settings=apps.settings.local
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
        self._cx_security(schools)
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

        self.stdout.write(self.style.MIGRATE_HEADING("Organogram (Codex internal, 25 seats)..."))
        staff = list(
            User.objects.filter(user_type="CX_STAFF", email__endswith="@vision.edu")
            .order_by("email")
        )
        if not staff:
            self.stdout.write(self.style.ERROR("  No vision staff — run seed_vision_staff first."))
            return

        def node(code, name, kind, parent=None):
            n, _ = OrgNode.objects.get_or_create(
                code=code, defaults=dict(name=name, kind=kind, parent=parent),
            )
            return n

        K = OrgNode.Kind
        exec_office = node("CX-EXEC", "Executive Office", K.DIVISION)
        eng = node("CX-ENG", "Engineering", K.DEPARTMENT, exec_office)
        cs = node("CX-CS", "Customer Success", K.DEPARTMENT, exec_office)
        growth = node("CX-GROWTH", "Growth & Partnerships", K.DEPARTMENT, exec_office)
        people = node("CX-PEOPLE", "People & Operations", K.DEPARTMENT, exec_office)
        platform_team = node("CX-ENG-PLAT", "Platform Team", K.TEAM, eng)
        product_team = node("CX-ENG-PROD", "Product Team", K.TEAM, eng)
        onboarding_team = node("CX-CS-ONB", "Onboarding Team", K.TEAM, cs)
        support_team = node("CX-CS-SUP", "Support Team", K.TEAM, cs)

        def seat(code, title, org_node, reports_to=None):
            pos, _ = Position.objects.get_or_create(
                code=code,
                defaults=dict(title=title, org_node=org_node, reports_to=reports_to),
            )
            return pos

        md = seat("CX-MD", "Managing Director", exec_office)
        eng_lead = seat("CX-ENG-LEAD", "Engineering Lead", eng, md)
        cs_lead = seat("CX-CS-LEAD", "Customer Success Lead", cs, md)
        growth_lead = seat("CX-GROWTH-LEAD", "Growth Lead", growth, md)
        people_lead = seat("CX-PEOPLE-LEAD", "People & Ops Lead", people, md)

        seats = [md, eng_lead, cs_lead, growth_lead, people_lead]
        seats += [seat(f"CX-ENG-PLAT-{i}", f"Software Engineer {i}", platform_team, eng_lead) for i in (1, 2, 3, 4)]
        seats += [seat(f"CX-ENG-PROD-{i}", t, product_team, eng_lead)
                  for i, t in ((1, "Product Designer"), (2, "Product Manager"), (3, "QA Engineer"))]
        seats += [seat(f"CX-CS-ONB-{i}", f"Onboarding Specialist {i}", onboarding_team, cs_lead) for i in (1, 2, 3)]
        seats += [seat(f"CX-CS-SUP-{i}", f"Support Officer {i}", support_team, cs_lead) for i in (1, 2, 3)]
        seats += [seat(f"CX-GROWTH-{i}", f"Partnerships Officer {i}", growth, growth_lead) for i in (1, 2, 3)]
        seats += [seat(f"CX-PEOPLE-{i}", t, people, people_lead)
                  for i, t in ((1, "HR Officer"), (2, "Operations Officer"),
                               (3, "Internal Accounts Officer"), (4, "Facilities Officer"))]

        for n, pos in ((exec_office, md), (eng, eng_lead), (cs, cs_lead),
                       (growth, growth_lead), (people, people_lead)):
            if n.head_position_id is None:
                n.head_position = pos
                n.save(update_fields=["head_position"])

        joined = self.now - timedelta(days=400)
        for idx, (user, pos) in enumerate(zip(staff, seats)):
            PositionAssignment.objects.get_or_create(
                user=user, position=pos,
                defaults=dict(is_primary=True, start_date=joined.date()),
            )
            PlatformStaffProfile.objects.get_or_create(
                user=user,
                defaults=dict(
                    employee_id=f"CX{idx + 1:04d}",
                    job_title=pos.title,
                    employment_type="FULL_TIME",
                    employment_status="ACTIVE",
                    date_joined=(joined + timedelta(days=idx * 9)).date(),
                    nationality="Nigerian",
                    city="Lagos",
                    state="Lagos",
                ),
            )

        # Platform roles: department leads run the backoffice day-to-day.
        self._platform_roles(staff, seats)
        self.stdout.write(
            f"  9 org nodes, {len(seats)} positions, {min(len(staff), len(seats))} seated staff with HR profiles."
        )

    def _platform_roles(self, staff, seats):
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment

        role = PlatformRoleTemplate.objects.filter(id="xvs_platform_admin").first()
        if role is None:
            return
        lead_codes = {"CX-MD", "CX-ENG-LEAD", "CX-CS-LEAD", "CX-GROWTH-LEAD", "CX-PEOPLE-LEAD"}
        granted = 0
        for user, pos in zip(staff, seats):
            if pos.code in lead_codes:
                _, created = PlatformUserRoleAssignment.objects.get_or_create(
                    user=user, role=role,
                    defaults=dict(assignment_status="ACTIVE"),
                )
                granted += int(created)
        self.stdout.write(f"  xvs_platform_admin granted to {granted} new lead(s).")

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

        self.stdout.write(self.style.MIGRATE_HEADING("ToDo board (CX staff)..."))
        seats = {
            pa.position.code: (pa.user, pa.position)
            for pa in PositionAssignment.objects.select_related("user", "position__org_node")
        }

        def u(code):
            entry = seats.get(code)
            return entry[0] if entry else None

        def dept(code):
            entry = seats.get(code)
            return entry[1].org_node.name if entry else ""

        D = timedelta
        # (assigner, assignee, title, priority, deadline-offset-days, done)
        specs = [
            ("CX-MD", "CX-ENG-LEAD", "Ship the academic core MVP", "HIGH", 21, False),
            ("CX-MD", "CX-CS-LEAD", "Prepare the pilot-school onboarding pack", "HIGH", 14, False),
            ("CX-MD", "CX-GROWTH-LEAD", "Close two pilot-school partnerships", "HIGH", 30, False),
            ("CX-MD", "CX-PEOPLE-LEAD", "Run Q3 performance reviews", "MEDIUM", 28, False),
            ("CX-ENG-LEAD", "CX-ENG-PLAT-1", "Profile slow dashboard endpoints", "MEDIUM", 7, False),
            ("CX-ENG-LEAD", "CX-ENG-PLAT-2", "Add OpenAPI schema generation", "LOW", 30, False),
            ("CX-ENG-LEAD", "CX-ENG-PLAT-3", "Rotate staging credentials", "HIGH", -3, False),
            ("CX-ENG-LEAD", "CX-ENG-PLAT-4", "Write the deploy runbook", "MEDIUM", -1, True),
            ("CX-ENG-LEAD", "CX-ENG-PROD-1", "Design the parent portal flows", "MEDIUM", 18, False),
            ("CX-ENG-LEAD", "CX-ENG-PROD-3", "Regression-test the import pipeline", "HIGH", 5, True),
            ("CX-CS-LEAD", "CX-CS-ONB-1", "Draft the onboarding checklist", "HIGH", 10, False),
            ("CX-CS-LEAD", "CX-CS-ONB-2", "Verify Greenfield data import", "MEDIUM", -2, False),
            ("CX-CS-LEAD", "CX-CS-SUP-1", "Triage open support threads", "HIGH", 2, False),
            ("CX-GROWTH-LEAD", "CX-GROWTH-1", "Prepare the Royal Crest demo", "MEDIUM", 6, True),
            ("CX-PEOPLE-LEAD", "CX-PEOPLE-1", "Collect updated staff documents", "LOW", 20, False),
            ("CX-PEOPLE-LEAD", "CX-PEOPLE-3", "Reconcile office running costs", "MEDIUM", 9, False),
        ]
        count = 0
        for assigner_code, assignee_code, title, priority, days, done in specs:
            assigner, assignee = u(assigner_code), u(assignee_code)
            if assigner is None or assignee is None:
                continue
            task, created = Task.objects.get_or_create(
                assignee=assignee, title=title,
                defaults=dict(
                    assigned_by=assigner,
                    assigned_by_name=assigner.full_name,
                    description="Seeded dev task.",
                    priority=priority,
                    department=dept(assignee_code),
                    deadline=(self.now + D(days=days)).date(),
                    is_done=done,
                    completed_at=self.now - D(days=1) if done else None,
                ),
            )
            count += int(created)
        self.stdout.write(f"  {count} tasks (mix of open, done and overdue).")

    # ------------------------------------------------------------------ #
    # 6b. CX security history + impersonation                            #
    # ------------------------------------------------------------------ #
    def _cx_security(self, schools):
        import uuid as _uuid

        from vs_admin_console.models import ImpersonationSession
        from vs_user.models import AuthAttempt, AuthEventLog, LoginSession, User

        self.stdout.write(self.style.MIGRATE_HEADING("CX security history..."))
        staff = list(
            User.objects.filter(user_type="CX_STAFF", email__endswith="@vision.edu")
            .order_by("email")[:6]
        )
        devices = [
            ("197.210.54.11", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Mac · Chrome"),
            ("105.112.99.34", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Windows · Edge"),
            ("41.184.122.7", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)", "iPhone · Safari"),
        ]
        sessions = attempts = 0
        for i, user in enumerate(staff):
            ip, ua, label = devices[i % len(devices)]
            if not LoginSession.objects.filter(user=user).exists():
                LoginSession.objects.create(
                    user=user, school=None, ip_address=ip, user_agent=ua,
                    device_label=label, refresh_jti=str(_uuid.uuid4()),
                    last_seen_at=self.now, is_active=True,
                )
                sessions += 1
            if not AuthAttempt.objects.filter(user=user).exists():
                AuthAttempt.objects.create(
                    email_entered=user.email, user=user, school=None,
                    result="SUCCESS", failure_code="", ip_address=ip, user_agent=ua,
                )
                if i % 2 == 0:
                    AuthAttempt.objects.create(
                        email_entered=user.email, user=user, school=None,
                        result="FAIL", failure_code="INVALID_CREDENTIALS",
                        ip_address=ip, user_agent=ua,
                    )
                AuthEventLog.objects.create(
                    actor=user, subject=user, school=None,
                    event="LOGIN_SUCCESS", ip_address=ip, user_agent=ua,
                )
                attempts += 1

        # One impersonation session: CS lead investigating a school issue.
        imp = 0
        cs_lead = staff[2] if len(staff) > 2 else staff[0]
        target_school = schools[0] if schools else None
        if target_school is not None:
            target_user = User.objects.filter(
                school=target_school, user_type="SCHOOL_ADMIN"
            ).first()
            if target_user and not ImpersonationSession.objects.filter(
                staff_user=cs_lead, school=target_school
            ).exists():
                ImpersonationSession.objects.create(
                    staff_user=cs_lead, school=target_school, target_user=target_user,
                    justification="Investigating reported import failure (seeded).",
                    started_at=self.now - timedelta(hours=2),
                    ends_at=self.now + timedelta(hours=1),
                )
                imp += 1
        self.stdout.write(f"  {sessions} login sessions, {attempts} staff with auth history, {imp} impersonation session.")

    # ------------------------------------------------------------------ #
    # 7. Audit events                                                    #
    # ------------------------------------------------------------------ #
    def _audit_events(self, schools):
        from vs_audit.services import emit_audit_event

        self.stdout.write(self.style.MIGRATE_HEADING("Audit events..."))
        emitted = 0
        for school in schools:
            event = emit_audit_event(
                module_key="SCHOOL",
                action_type="CREATE",
                entity_type="School",
                entity_id=str(school.pk),
                entity_label=school.name,
                summary=f"Seeded school {school.name}.",
            )
            # emit_audit_event is best-effort and returns None on failure —
            # count real successes so this line can't overstate.
            emitted += int(event is not None)
        self.stdout.write(f"  {emitted}/{len(schools)} events emitted.")
