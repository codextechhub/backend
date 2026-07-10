"""
seed_dev_data — one command that fills a fresh dev database with a connected
world covering every module EXCEPT finance/procurement/payments.

FOCUS: CX (Codex) staff are the main subjects — the platform currently runs
as the Codex staff intranet. The seeder builds a complete 40-seat company
(7-level classic corporate hierarchy: MD → C-Suite → Directors → Managers →
Team Leads → Seniors → ICs; rich HR profiles, platform roles, todo board,
login/security history, impersonation sessions). The schools exist as the
CUSTOMER BASE those staff manage, not as the protagonists.

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
./reseed-dev.sh
---OR---
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

    # Varied Nigerian cities so HR profiles look realistic.
    _STAFF_CITIES = [
        ("Lagos", "Lagos"), ("Abuja", "FCT"), ("Port Harcourt", "Rivers"),
        ("Ibadan", "Oyo"), ("Enugu", "Enugu"), ("Kano", "Kano"),
        ("Benin City", "Edo"), ("Owerri", "Imo"),
    ]

    def _organogram(self):
        from vs_user.models import (
            OrgNode, PlatformStaffProfile, Position, PositionAssignment, User,
        )

        self.stdout.write(self.style.MIGRATE_HEADING("Organogram (Codex internal, 40 seats / 7 levels)..."))
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

        # ---------- Org nodes (10 total: 1 division, 4 departments, 5 teams) ----------
        exec_office     = node("CX-EXEC",       "Executive Office",      K.DIVISION)
        technology      = node("CX-TECH",       "Technology",            K.DEPARTMENT, exec_office)
        cs_dept         = node("CX-CS",         "Customer Success",      K.DEPARTMENT, exec_office)
        growth_dept     = node("CX-GROWTH",     "Growth & Partnerships", K.DEPARTMENT, exec_office)
        people_dept     = node("CX-PEOPLE",     "People & Finance",      K.DEPARTMENT, exec_office)
        platform_team   = node("CX-TECH-PLAT",  "Platform Engineering",  K.TEAM, technology)
        product_team    = node("CX-TECH-PROD",  "Product & Design",      K.TEAM, technology)
        onboarding_team = node("CX-CS-ONB",     "Onboarding",            K.TEAM, cs_dept)
        support_team    = node("CX-CS-SUP",     "Support",               K.TEAM, cs_dept)
        growth_team     = node("CX-GROWTH-ANA", "Growth Analytics",      K.TEAM, growth_dept)

        def seat(code, title, org_node, reports_to=None):
            pos, _ = Position.objects.get_or_create(
                code=code,
                defaults=dict(title=title, org_node=org_node, reports_to=reports_to),
            )
            return pos

        # L1
        md = seat("CX-MD", "Managing Director", exec_office)

        # L2 — C-Suite (report to MD)
        cto = seat("CX-CTO", "Chief Technology Officer",   exec_office, md)
        coo = seat("CX-COO", "Chief Operating Officer",    exec_office, md)
        cfo = seat("CX-CFO", "Chief Financial Officer",    exec_office, md)
        cpo = seat("CX-CPO", "Chief Partnerships Officer", exec_office, md)

        # L3 — Directors (report to respective C-Suite)
        dir_eng    = seat("CX-DIR-ENG",    "Director of Engineering",      technology,  cto)
        dir_cs     = seat("CX-DIR-CS",     "Director of Customer Success", cs_dept,     coo)
        dir_growth = seat("CX-DIR-GROWTH", "Director of Growth",           growth_dept, cpo)
        dir_people = seat("CX-DIR-PEOPLE", "Director of People",           people_dept, cfo)

        # L4 — Managers (report to respective Directors)
        mgr_eng    = seat("CX-MGR-ENG",    "Engineering Manager",      technology,  dir_eng)
        mgr_cs     = seat("CX-MGR-CS",     "CS Manager",               cs_dept,     dir_cs)
        mgr_growth = seat("CX-MGR-GROWTH", "Growth Manager",           growth_dept, dir_growth)
        mgr_people = seat("CX-MGR-PEOPLE", "People Manager",           people_dept, dir_people)

        # L5 — Team Leads (report to respective Managers)
        lead_plat   = seat("CX-LEAD-PLAT",   "Platform Team Lead",   platform_team,   mgr_eng)
        lead_prod   = seat("CX-LEAD-PROD",   "Product Team Lead",    product_team,    mgr_eng)
        lead_onb    = seat("CX-LEAD-ONB",    "Onboarding Team Lead", onboarding_team, mgr_cs)
        lead_sup    = seat("CX-LEAD-SUP",    "Support Team Lead",    support_team,    mgr_cs)
        lead_growth = seat("CX-LEAD-GROWTH", "Growth Team Lead",     growth_team,     mgr_growth)

        # L6 — Seniors (report to respective Team Leads or People Manager)
        sr_plat1   = seat("CX-SR-PLAT-1",   "Senior Platform Engineer I",      platform_team,   lead_plat)
        sr_plat2   = seat("CX-SR-PLAT-2",   "Senior Platform Engineer II",     platform_team,   lead_plat)
        sr_prod1   = seat("CX-SR-PROD-1",   "Senior Product Designer",         product_team,    lead_prod)
        sr_onb1    = seat("CX-SR-ONB-1",    "Senior Onboarding Specialist I",  onboarding_team, lead_onb)
        sr_onb2    = seat("CX-SR-ONB-2",    "Senior Onboarding Specialist II", onboarding_team, lead_onb)
        sr_sup1    = seat("CX-SR-SUP-1",    "Senior Support Officer",          support_team,    lead_sup)
        sr_growth1 = seat("CX-SR-GROWTH-1", "Senior Growth Analyst I",         growth_team,     lead_growth)
        sr_growth2 = seat("CX-SR-GROWTH-2", "Senior Growth Analyst II",        growth_team,     lead_growth)
        sr_hr1     = seat("CX-SR-HR-1",     "Senior HR Officer",               people_dept,     mgr_people)
        sr_ops1    = seat("CX-SR-OPS-1",    "Senior Operations Officer",       people_dept,     mgr_people)

        # L7 — Individual Contributors (report to their respective Senior)
        plat1   = seat("CX-PLAT-1",   "Platform Engineer I",      platform_team,   sr_plat1)
        plat2   = seat("CX-PLAT-2",   "Platform Engineer II",     platform_team,   sr_plat1)
        plat3   = seat("CX-PLAT-3",   "Platform Engineer III",    platform_team,   sr_plat2)
        prod1   = seat("CX-PROD-1",   "Product Manager",          product_team,    sr_prod1)
        prod2   = seat("CX-PROD-2",   "QA Engineer",              product_team,    sr_prod1)
        onb1    = seat("CX-ONB-1",    "Onboarding Specialist I",  onboarding_team, sr_onb1)
        onb2    = seat("CX-ONB-2",    "Onboarding Specialist II", onboarding_team, sr_onb2)
        sup1    = seat("CX-SUP-1",    "Support Officer I",        support_team,    sr_sup1)
        sup2    = seat("CX-SUP-2",    "Support Officer II",       support_team,    sr_sup1)
        growth1 = seat("CX-GROWTH-1", "Partnerships Officer I",  growth_team,     sr_growth1)
        growth2 = seat("CX-GROWTH-2", "Partnerships Officer II", growth_team,     sr_growth2)
        hr1     = seat("CX-HR-1",     "HR Officer",               people_dept,     sr_hr1)

        # Ordered L1 → L7 so staff[0] (seniority by email sort) gets the MD seat.
        seats = [
            md,
            cto, coo, cfo, cpo,
            dir_eng, dir_cs, dir_growth, dir_people,
            mgr_eng, mgr_cs, mgr_growth, mgr_people,
            lead_plat, lead_prod, lead_onb, lead_sup, lead_growth,
            sr_plat1, sr_plat2, sr_prod1, sr_onb1, sr_onb2,
            sr_sup1, sr_growth1, sr_growth2, sr_hr1, sr_ops1,
            plat1, plat2, plat3, prod1, prod2,
            onb1, onb2, sup1, sup2, growth1, growth2, hr1,
        ]  # 1+4+4+4+5+10+12 = 40

        for n, pos in (
            (exec_office, md),
            (technology, dir_eng),
            (cs_dept, dir_cs),
            (growth_dept, dir_growth),
            (people_dept, dir_people),
            (platform_team, lead_plat),
            (product_team, lead_prod),
            (onboarding_team, lead_onb),
            (support_team, lead_sup),
            (growth_team, lead_growth),
        ):
            if n.head_position_id is None:
                n.head_position = pos
                n.save(update_fields=["head_position"])

        # Find max existing CX#### so re-runs never collide with old profiles.
        import re as _re
        existing_ids = PlatformStaffProfile.objects.values_list("employee_id", flat=True)
        max_num = max(
            (int(m.group(1)) for eid in existing_ids if (m := _re.match(r"CX(\d+)$", eid or ""))),
            default=0,
        )
        next_emp_num = max_num + 1

        joined = self.now - timedelta(days=400)
        for idx, (user, pos) in enumerate(zip(staff, seats)):
            PositionAssignment.objects.get_or_create(
                user=user, position=pos,
                defaults=dict(is_primary=True, start_date=joined.date()),
            )
            city, state = self._STAFF_CITIES[idx % len(self._STAFF_CITIES)]
            profile, created = PlatformStaffProfile.objects.get_or_create(
                user=user,
                defaults=dict(
                    employee_id=f"CX{next_emp_num:04d}",
                    job_title=pos.title,
                    position=pos,
                    employment_type="FULL_TIME",
                    employment_status="ACTIVE",
                    date_joined=(joined + timedelta(days=idx * 9)).date(),
                    nationality="Nigerian",
                    city=city,
                    state=state,
                ),
            )
            if created:
                next_emp_num += 1
            elif profile.position_id is None:
                # Backfill profiles created by the old seeder without position set.
                profile.position = pos
                profile.save(update_fields=["position", "updated_at"])

        self._platform_roles(staff, seats)
        seated = min(len(staff), len(seats))
        self.stdout.write(
            f"  10 org nodes, {len(seats)} positions (7 levels), {seated} seated staff with HR profiles."
        )

    def _platform_roles(self, staff, seats):
        from vs_rbac.models import PlatformRoleTemplate, PlatformUserRoleAssignment

        role = PlatformRoleTemplate.objects.filter(id="xvs_platform_admin").first()
        if role is None:
            return
        # Grant platform-admin access to the MD and all C-Suite (L1–L2).
        lead_codes = {"CX-MD", "CX-CTO", "CX-COO", "CX-CFO", "CX-CPO"}
        granted = 0
        for user, pos in zip(staff, seats):
            if pos.code in lead_codes:
                _, created = PlatformUserRoleAssignment.objects.get_or_create(
                    user=user, role=role,
                    defaults=dict(assignment_status="ACTIVE"),
                )
                granted += int(created)
        self.stdout.write(f"  xvs_platform_admin granted to {granted} new C-Suite member(s).")

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
                from vs_config.models import Capability, CapabilityEntitlement
                modules = Capability.objects.filter(kind=Capability.Kind.MODULE).exclude(
                    key__in=["finance", "procurement", "payments", "vendors"]
                )
                for capability in modules:
                    CapabilityEntitlement.all_objects.update_or_create(
                        capability=capability,
                        scope_key=f"school:{school.pk}",
                        defaults={
                            "school": school, "state": "GRANTED", "source": "PACKAGE"
                        },
                    )
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

        self.stdout.write(self.style.MIGRATE_HEADING("ToDo board (CX staff — all 7 levels)..."))
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
        # (assigner_code, assignee_code, title, priority, deadline-offset-days, done)
        # Spans all 7 levels — every manager assigns at least one task to each direct report.
        specs = [
            # L1 → L2 (MD assigns to C-Suite)
            ("CX-MD",  "CX-CTO", "Finalise the Q3 product and engineering roadmap",   "HIGH",   21, False),
            ("CX-MD",  "CX-COO", "Set up the pilot school operations playbook",        "HIGH",   14, False),
            ("CX-MD",  "CX-CFO", "Prepare the Q2 financial performance summary",       "HIGH",   10, False),
            ("CX-MD",  "CX-CPO", "Identify five new school partnership leads",         "MEDIUM", 30, False),
            # L2 → L3 (C-Suite assigns to Directors)
            ("CX-CTO", "CX-DIR-ENG",    "Review and approve the platform architecture",  "HIGH",   14, False),
            ("CX-COO", "CX-DIR-CS",     "Review Q2 NPS scores and produce action plan",  "MEDIUM", 10, False),
            ("CX-CPO", "CX-DIR-GROWTH", "Prepare partnership deck for regional schools", "MEDIUM", 20, False),
            ("CX-CFO", "CX-DIR-PEOPLE", "Finalise the staff handbook first draft",       "MEDIUM", 28, False),
            # L3 → L4 (Directors assign to Managers)
            ("CX-DIR-ENG",    "CX-MGR-ENG",    "Complete the engineering Q3 hiring plan",  "HIGH",   7,  False),
            ("CX-DIR-CS",     "CX-MGR-CS",     "Map the end-to-end school onboarding journey", "HIGH", 12, False),
            ("CX-DIR-GROWTH", "CX-MGR-GROWTH", "Score the top 10 partnership prospects",  "MEDIUM", 15, False),
            ("CX-DIR-PEOPLE", "CX-MGR-PEOPLE", "Audit current staff leave balances",       "LOW",    20, False),
            # L4 → L5 (Managers assign to Team Leads)
            ("CX-MGR-ENG",    "CX-LEAD-PLAT",   "Plan the microservices migration sprint",  "HIGH",   10, False),
            ("CX-MGR-ENG",    "CX-LEAD-PROD",   "Run user research interviews at schools",  "MEDIUM", 18, False),
            ("CX-MGR-CS",     "CX-LEAD-ONB",    "Write the school onboarding SOP document", "HIGH",   7,  False),
            ("CX-MGR-CS",     "CX-LEAD-SUP",    "Analyse support ticket backlog by type",   "MEDIUM", 5,  False),
            ("CX-MGR-GROWTH", "CX-LEAD-GROWTH", "Run the Q2 partnership pipeline review",   "HIGH",   6,  True),
            # L5 → L6 (Team Leads assign to Seniors)
            ("CX-LEAD-PLAT",   "CX-SR-PLAT-1",   "Refactor the authentication middleware",       "HIGH",   5,  False),
            ("CX-LEAD-PLAT",   "CX-SR-PLAT-2",   "Optimise slow dashboard DB queries",           "MEDIUM", 7,  False),
            ("CX-LEAD-PROD",   "CX-SR-PROD-1",   "Design the parent portal wireframes",          "MEDIUM", 18, False),
            ("CX-LEAD-ONB",    "CX-SR-ONB-1",    "Conduct Greenfield Academy training session",  "HIGH",   3,  True),
            ("CX-LEAD-ONB",    "CX-SR-ONB-2",    "Build the school onboarding template pack",    "MEDIUM", 12, False),
            ("CX-LEAD-SUP",    "CX-SR-SUP-1",    "Write the support escalation playbook",        "HIGH",   4,  False),
            ("CX-LEAD-GROWTH", "CX-SR-GROWTH-1", "Profile the top three partnership prospects",  "MEDIUM", 9,  False),
            ("CX-LEAD-GROWTH", "CX-SR-GROWTH-2", "Prepare the demo deck for Royal Crest",        "HIGH",   3,  True),
            # L6 → L7 (Seniors assign to Individual Contributors)
            ("CX-SR-PLAT-1",   "CX-PLAT-1",   "Fix pagination bug in the reports API",         "MEDIUM", 3,  False),
            ("CX-SR-PLAT-1",   "CX-PLAT-2",   "Add unit tests for the import pipeline",        "MEDIUM", 7,  False),
            ("CX-SR-PLAT-2",   "CX-PLAT-3",   "Rotate staging environment credentials",        "HIGH",   -3, False),
            ("CX-SR-PROD-1",   "CX-PROD-1",   "Write product spec for the school fee module",  "MEDIUM", 14, False),
            ("CX-SR-PROD-1",   "CX-PROD-2",   "Regression-test the student import flow",       "HIGH",   5,  True),
            ("CX-SR-ONB-1",    "CX-ONB-1",    "Upload Unity Heights student bulk data",         "HIGH",   2,  False),
            ("CX-SR-ONB-2",    "CX-ONB-2",    "Send welcome emails to Royal Crest admins",     "LOW",    4,  False),
            ("CX-SR-SUP-1",    "CX-SUP-1",    "Resolve open school admin support ticket",      "HIGH",   1,  False),
            ("CX-SR-SUP-1",    "CX-SUP-2",    "Update help-centre FAQ articles",               "LOW",    10, False),
            ("CX-SR-GROWTH-1", "CX-GROWTH-1", "Draft intro email for new partnership lead",    "MEDIUM", 5,  False),
            ("CX-SR-GROWTH-2", "CX-GROWTH-2", "Research school fees data for pitch deck",      "LOW",    8,  False),
            ("CX-SR-HR-1",     "CX-HR-1",     "Collect updated staff identification docs",     "LOW",    15, False),
        ]
        count = 0
        for assigner_code, assignee_code, title, priority, days, done in specs:
            assigner, assignee = u(assigner_code), u(assignee_code)
            if assigner is None or assignee is None:
                continue
            _, created = Task.objects.get_or_create(
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
        self.stdout.write(f"  {count} tasks across 7 levels (mix of open, done, and overdue).")

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
