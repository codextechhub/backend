from django.core.management.base import BaseCommand
from django.db import transaction

from vs_config.models import Capability, CapabilityDependency, ConfigurationDefinition


# (key, label, description, value_type, default_value, validation_rules)
DEFINITIONS = [
    (
        "notifications.email_max_retries", "Email Delivery Max Retries",
        "Maximum delivery attempts for a queued email notification.",
        "INTEGER", 3, {"min": 0, "max": 10},
    ),
    (
        "notifications.email_retry_backoff_seconds", "Email Retry Backoff Seconds",
        "Base backoff in seconds between email delivery retries.",
        "INTEGER", 60, {"min": 1, "max": 3600},
    ),
    (
        "security.failed_login_threshold", "Failed Login Threshold",
        "Failed password attempts allowed before the account is locked.",
        "INTEGER", 5, {"min": 3, "max": 20},
    ),
    (
        "security.account_lock_minutes", "Account Lock Duration",
        "Minutes an account remains locked after reaching the failed-login threshold.",
        "INTEGER", 15, {"min": 5, "max": 1440},
    ),
    (
        "security.self_reset_expiry_hours", "Self-service Reset Lifetime",
        "Hours a self-service password reset link remains valid.",
        "INTEGER", 1, {"min": 1, "max": 24},
    ),
    (
        "security.admin_reset_expiry_hours", "Admin Reset Lifetime",
        "Hours an administrator-triggered password reset link remains valid.",
        "INTEGER", 24, {"min": 1, "max": 168},
    ),
    (
        "security.invitation_expiry_days", "Invitation Lifetime",
        "Days a new-user invitation remains valid, including after resend.",
        "INTEGER", 7, {"min": 1, "max": 30},
    ),
    (
        "security.proxy_idle_timeout_minutes", "Proxy Session Idle Timeout",
        "Minutes an open-ended proxy session may remain idle before it expires.",
        "INTEGER", 30, {"min": 5, "max": 120},
    ),
    (
        "integrations.email.sender_name", "Default Email Sender Name",
        "Display name used for platform email when a message does not provide its own sender name.",
        "STRING", None, {},
    ),
    (
        "integrations.email.sender_address", "Default Email Sender Address",
        "From address used for platform email. SMTP credentials remain deployment-owned.",
        "STRING", None, {},
    ),
    (
        "platform.profile.name", "Platform Name",
        "Name printed when the platform is the issuer of a Finance document.",
        "STRING", None, {},
    ),
    (
        "platform.profile.tagline", "Platform Tagline",
        "Short line printed below the platform name on issued documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.address", "Platform Address",
        "Contact address printed on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.email", "Platform Email",
        "Contact email printed on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.phone", "Platform Phone",
        "Contact phone printed on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.website", "Platform Website",
        "Website printed on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.profile.logo_url", "Platform Logo URL",
        "Public logo URL used on platform-issued Finance documents.",
        "STRING", None, {},
    ),
    (
        "platform.entitlements.enforce", "Enforce Plan Entitlements",
        "Whether a request is refused when the school's plan does not reach "
        "the capability behind the permission being used. On, so an environment "
        "built from this catalogue behaves like the ones already running it. It "
        "stays readable per school, so a school can be excused from enforcement "
        "without switching it off for the platform.",
        "BOOLEAN", True, {},
    ),
    (
        "platform.onboarding.default_ownership_type", "Default School Ownership",
        "Ownership type preselected when a new school omits the field.",
        "CHOICE", "PUBLIC", {"choices": ["PUBLIC", "PRIVATE", "FAITH_BASED", "NGO"]},
    ),
    (
        "platform.onboarding.default_term_structure", "Default Academic Structure",
        "Academic calendar structure used when a new school omits the field.",
        "CHOICE", "3_TERMS", {"choices": ["2_SEMESTERS", "3_TERMS"]},
    ),
    (
        "platform.onboarding.default_currency", "Default School Currency",
        "Billing currency used when a new school omits the field.",
        "CHOICE", "NGN", {"choices": ["NGN", "USD"]},
    ),
    (
        "platform.onboarding.default_branch_country", "Default Branch Country",
        "Country used when a new branch omits the field.",
        "STRING", "Nigeria", {},
    ),
    (
        "payments.held_reconciliation_tolerance", "Held-ledger Reconciliation Tolerance",
        "Naira by which the payment provider's reported balance may differ from the "
        "platform's books before the daily check opens a health incident. A whole "
        "number of naira; 0 means the two must agree exactly.",
        "INTEGER", 0, {"min": 0, "max": 1_000_000},
    ),
    (
        "payments.provider_balance_swept", "Paystack Balance Swept Automatically",
        "Whether Paystack settles the platform's own balance to the platform's bank by "
        "automatic settlement. On, the daily held-ledger check counts each settlement once "
        "and allows for it; off, it compares the balance as reported. Changed only from the "
        "payment provider settings, by platform staff holding its update permission.",
        "BOOLEAN", False, {},
    ),
]

# (key, label, description, value_type, default_value, validation_rules)
#
# Settings a SCHOOL may override. Held in their own table rather than behind a
# seventh tuple element, so the platform-only list above cannot acquire a
# school scope through a typo in a column nobody reads.
SCHOOL_SCOPED_DEFINITIONS = [
    (
        "students.admission_number.required", "Admission Number Required",
        "Whether every student at this school must be given an admission "
        "number when they are enrolled.",
        "BOOLEAN", False, {},
    ),
    (
        "students.admission_number.pattern", "Admission Number Pattern",
        "A regular expression every admission number at this school must "
        "match. Anchored by the server, so it cannot match part of a longer "
        "number. Empty means any shape is accepted.",
        "STRING", "", {},
    ),
    (
        "students.admission_number.hint", "Admission Number Hint",
        "The sentence shown under the admission number field, and quoted "
        "verbatim when a number is refused. This is the only one of the three "
        "a person reads, which is why a refusal never quotes the pattern.",
        "STRING", "", {},
    ),
    (
        "students.admission_number.auto_issue", "Issue Admission Numbers Automatically",
        "Whether a student enrolled or confirmed with no admission number is "
        "given the next number in the school's series, or the branch's where "
        "the branch has its own rule.",
        "BOOLEAN", False, {},
    ),
    (
        "students.age.min_years", "Youngest Enrolment Age",
        "The youngest a student at this school can be, in whole years. A "
        "birth date that makes a child younger is refused as a mistyped "
        "year, on the enrolment form, the edit form and the import.",
        "INTEGER", 2, {"min": 0, "max": 99},
    ),
    (
        "students.age.max_years", "Oldest Enrolment Age",
        "The oldest a student at this school can be, in whole years. A "
        "birth date that makes a child older is refused as a mistyped year.",
        "INTEGER", 25, {"min": 0, "max": 99},
    ),
    (
        "students.documents.required", "Required Student Documents",
        "The documents a student's checklist marks as required. A prompt, "
        "never a gate: a child is enrolled whether or not they are attached.",
        "JSON", ["BIRTH_CERTIFICATE"], {},
    ),
    (
        "students.enrolment.required_fields", "Required Enrolment Fields",
        "Optional enrolment fields this school requires, such as nationality "
        "or home address. Enforced when a student is enrolled or imported, "
        "and when an edit would blank one; an older record missing one is "
        "never blocked.",
        "JSON", [], {},
    ),
    (
        "students.capacity.mode", "Class Capacity Rule",
        "What a full class does. WARN refuses until staff choose to go "
        "ahead; HARD refuses with no override; OFF does not check.",
        "CHOICE", "WARN", {"choices": ["WARN", "HARD", "OFF"]},
    ),
    (
        "students.capacity.default", "Default Class Size",
        "The capacity a new class is given when it is created without one. "
        "Empty means no limit.",
        "INTEGER", None, {"min": 1, "max": 500},
    ),
    (
        "guardians.min_per_student", "Guardians Per Student",
        "How many guardians every child at this school needs. Enrolment and "
        "saving an applicant refuse fewer, and a guardian cannot be removed "
        "from a child on the roll if that would leave fewer. The student "
        "import still imports its one guardian per child, with a warning.",
        "INTEGER", 1, {"min": 1, "max": 4},
    ),
    (
        "guardians.email_required", "Guardian Email Required",
        "Whether a new guardian must be given an email address, on the "
        "enrolment form, when linking, and in both imports, and whether an "
        "edit may blank one. A guardian already held with no email can still "
        "be linked to another child.",
        "BOOLEAN", False, {},
    ),
    (
        "guardians.matching", "Guardian Matching",
        "How a guardian typed in is recognised as one this school already "
        "holds. EMAIL_THEN_PHONE matches on email, then on phone; EMAIL_ONLY "
        "never matches on phone, for a school whose families share landlines.",
        "CHOICE", "EMAIL_THEN_PHONE", {"choices": ["EMAIL_THEN_PHONE", "EMAIL_ONLY"]},
    ),
    (
        "guardians.relationships.extra", "Additional Guardian Relationships",
        "Relationships this school records beyond the fixed eight, such as "
        "Sponsor or Driver: up to 10, each up to 30 characters. A link stores "
        "one as Other with the school's label, and removing it from this list "
        "leaves those links as they are.",
        "JSON", [], {},
    ),
    (
        "applicants.documents.required_to_confirm",
        "Documents Required To Confirm An Applicant",
        "The documents a child must have on their record before joining the roll: "
        "an applicant before being confirmed as enrolled, on every route that "
        "confirms one, and a child enrolled directly, whose documents are sent "
        "with the enrolment. At a school with any, the student import brings each "
        "row in as an applicant. Empty means nothing waits for a document.",
        "JSON", [], {},
    ),
    (
        "students.promotion.suspended", "Suspended Pupils At Promotion",
        "What the end-of-year promotion does with a suspended pupil. HOLD "
        "lists them as an exception and leaves them where they are; PROMOTE "
        "moves them up with their year group, still suspended.",
        "CHOICE", "HOLD", {"choices": ["HOLD", "PROMOTE"]},
    ),
    (
        "students.promotion.not_placed", "Unplaced Pupils At Promotion",
        "What the end-of-year promotion does with a pupil who is confirmed "
        "but not placed and still holds a class in the year being left. HOLD "
        "leaves them there; PROMOTE moves them up with that class.",
        "CHOICE", "HOLD", {"choices": ["HOLD", "PROMOTE"]},
    ),
    (
        "students.promotion.arms", "Arms At Promotion",
        "Which of next year's classes a promoted pupil joins. SAME_ARM keeps "
        "an arm together (JSS1 B to JSS2 B); SPREAD shares the pupils moving "
        "into a level evenly across its classes, emptiest first.",
        "CHOICE", "SAME_ARM", {"choices": ["SAME_ARM", "SPREAD"]},
    ),
    (
        "students.promotion.capacity_mode", "Class Capacity At Promotion",
        "What the end-of-year promotion does when it would fill a class past "
        "its capacity. FOLLOW_ENROLMENT applies the Class Capacity Rule; WARN, "
        "HARD and OFF mean what they mean there, for the promotion alone.",
        "CHOICE", "FOLLOW_ENROLMENT",
        {"choices": ["FOLLOW_ENROLMENT", "WARN", "HARD", "OFF"]},
    ),
    (
        "students.suspension.notice", "Who Is Told When A Pupil Is Suspended",
        "Who the school writes to when a pupil is suspended. "
        "PRIMARY_GUARDIAN tells the one guardian marked as the pupil's main "
        "contact; ALL_GUARDIANS tells every guardian on the pupil's record; "
        "NOBODY sends nothing, for a school that tells families itself. A "
        "guardian the school holds no email address and no account for cannot "
        "be written to, and nobody is written to in their place.",
        "CHOICE", "PRIMARY_GUARDIAN",
        {"choices": ["PRIMARY_GUARDIAN", "ALL_GUARDIANS", "NOBODY"]},
    ),
    (
        "staff.number.required", "Staff Number Required",
        "Whether every new member of staff must be given a staff number, on "
        "the Add form, on the import and when an edit would blank one.",
        "BOOLEAN", False, {},
    ),
    (
        "staff.number.pattern", "Staff Number Pattern",
        "A regular expression every new staff number must match. Anchored by "
        "the server, so it cannot match part of a longer number. Empty means "
        "any shape is accepted.",
        "STRING", "", {},
    ),
    (
        "staff.number.hint", "Staff Number Hint",
        "The sentence shown under the staff number field, and quoted "
        "verbatim when a number is refused.",
        "STRING", "", {},
    ),
    (
        "staff.number.auto_issue", "Issue Staff Numbers Automatically",
        "Whether a member of staff added with no staff number is given the "
        "next number in the school's series, or the branch's where the "
        "branch has its own rule. A number once held is never issued again.",
        "BOOLEAN", False, {},
    ),
    (
        "staff.starting_role", "Starting Role For New Staff",
        "The key of the role every member of staff added at a live school "
        "starts with. It must be one of the school's active roles.",
        "STRING", "teacher", {},
    ),
    (
        "staff.documents.required", "Required Staff Documents",
        "The document types this school expects on every staff record. A "
        "record missing one is flagged; nothing is refused.",
        "JSON", [], {},
    ),
    (
        "staff.self_editable_fields", "Fields Staff May Edit Themselves",
        "The details a member of staff may change on their own record. The "
        "staff number, job title, employment type, hire and exit dates, "
        "email and posting are never among them.",
        "JSON", ["middle_name", "date_of_birth", "photo", "phone"], {},
    ),
    (
        "staff.hire.requires_approval", "Approve New Staff Before Inviting",
        "Whether a member of staff added at this school waits for the New "
        "staff approval ladder before their invitation is sent.",
        "BOOLEAN", False, {},
    ),
    (
        "staff.leave.allowances", "Leave Allowances",
        "Days of each leave type a member of staff may take in one academic "
        "session, keyed by leave type. A type left out has no limit. Leave "
        "past its allowance is still filed, marked for the approver.",
        "JSON", {}, {},
    ),
    (
        "staff.leave.groups", "Staff Leave Groups",
        "Named groups assigned to staff for leave allowance exceptions.",
        "JSON", [], {},
    ),
    (
        "staff.leave.overrides", "Leave Allowance Exceptions",
        "Leave type allowances for a branch, a staff leave group, or both.",
        "JSON", [], {},
    ),
    (
        "staff.leave.working_days", "Working Days For Leave",
        "The weekdays a leave request counts, as ISO numbers (Monday is 1, "
        "Sunday is 7).",
        "JSON", [1, 2, 3, 4, 5], {},
    ),
    (
        "staff.leave.exclude_closures", "Leave Skips School Closures",
        "Whether a day the school calendar closes the school, at the "
        "person's branch or school-wide, is left out of a leave request's "
        "count.",
        "BOOLEAN", True, {},
    ),
    (
        "academics.terms.word", "Word For A Term",
        "What the school calls the parts of its year, TERM or SEMESTER, in "
        "every sentence it is shown. Empty means the word the school's term "
        "structure implies. Changing it never renames a term.",
        "CHOICE", None, {"choices": ["TERM", "SEMESTER"]},
    ),
    (
        "academics.terms.names", "Term Names",
        "The names a new academic year's terms are given, in order: one to "
        "six, each at most 30 characters, none repeated. Empty means the names "
        "the school's term structure implies.",
        "JSON", None, {},
    ),
    (
        "academics.classes.default_arms", "Default Class Arms",
        "The arms a level's classes are generated with, in order: one to "
        "twelve, each at most 30 characters, none repeated. A class is named "
        "after its level and arm, such as JSS1 A.",
        "JSON", ["A", "B", "C"], {},
    ),
    (
        "calendar.teaching_days", "Teaching Days",
        "The weekdays the school teaches, as ISO numbers (Monday is 1, Sunday "
        "is 7), at least one. They are the day columns of every timetable, "
        "the days a period or a lesson may be placed on, and the days the "
        "calendar counts as taught.",
        "JSON", [1, 2, 3, 4, 5], {},
    ),
    (
        "calendar.week_starts_on", "Week Starts On",
        "The day the school's week starts on: 1 for Monday, 7 for Sunday. "
        "Calendars and timetables start their week on it.",
        "CHOICE", 1, {"choices": [1, 7]},
    ),
    (
        "calendar.closes_school_by_type", "Entries That Close The School",
        "For each kind of calendar entry, whether an entry of that kind closes "
        "the school when it is created without saying. An existing entry "
        "never changes.",
        "JSON",
        {
            "HOLIDAY": True, "MIDTERM_BREAK": True, "EXAM_PERIOD": False,
            "SCHOOL_EVENT": False, "PTA": False, "SPORTS": False,
        },
        {},
    ),
    (
        "timetable.room_required_to_publish", "Lessons Need A Room To Publish",
        "Whether every lesson needs a room before a class timetable can be "
        "published. A lesson always needs a teacher.",
        "BOOLEAN", True, {},
    ),
    (
        "timetable.teacher_duty_match", "Teacher Must Hold The Teaching Duty",
        "What happens to a lesson whose teacher has no teaching duty for its "
        "class and subject: OFF allows it, WARN saves it with a warning, "
        "REFUSE refuses it and blocks publishing while one remains.",
        "CHOICE", "OFF", {"choices": ["OFF", "WARN", "REFUSE"]},
    ),
    (
        "exams.invigilator_roles", "Roles That May Invigilate",
        "The keys of the school's roles whose active holders may invigilate "
        "an exam paper.",
        "JSON", ["teacher"], {},
    ),
    (
        "timetable.default_period_minutes", "Default Period Length",
        "The minutes a new period lasts unless its end time is changed, 10 to "
        "240. Empty means no default.",
        "INTEGER", None, {"min": 10, "max": 240},
    ),
    (
        "display.timezone", "Time Zone",
        "The IANA time zone this school keeps its calendar in, such as "
        "Africa/Lagos. It decides which day \"today\" is for due dates, overdue "
        "checks, attendance and the calendar. A branch in another zone may keep "
        "its own, and anything belonging to that branch then follows the "
        "branch's day. The platform value is the default for every school.",
        "STRING", "Africa/Lagos", {},
    ),
    (
        "display.date_format", "Date Format",
        "How the school's screens write a date: D_MMM_YYYY (29 Sep 2026), "
        "DD_MM_YYYY (29/09/2026) or YYYY_MM_DD (2026-09-29).",
        "CHOICE", "D_MMM_YYYY",
        {"choices": ["D_MMM_YYYY", "DD_MM_YYYY", "YYYY_MM_DD"]},
    ),
    (
        "display.clock", "Clock",
        "How the school's screens write a time of day: H12 (8:00 am) or H24 "
        "(08:00).",
        "CHOICE", "H12", {"choices": ["H12", "H24"]},
    ),
]

#: School-scoped settings a BRANCH may also hold its own value of. Named here
#: rather than widened for every school setting, because most of them are
#: facts about the school that a branch answering differently would contradict.
#: A branch's admission-number rule replaces the school's for its students, its
#: staff-number rule the school's for the staff posted there, and its time zone
#: the school's for everything that belongs to it.
BRANCH_OVERRIDABLE = frozenset({
    "display.timezone",
    "students.admission_number.required",
    "students.admission_number.pattern",
    "students.admission_number.hint",
    "students.admission_number.auto_issue",
    "staff.number.required",
    "staff.number.pattern",
    "staff.number.hint",
    "staff.number.auto_issue",
})

#: The modules a school is sold. Every school is granted every one of them;
#: the plan it pays for decides how far into each it reaches. Nothing here is
#: withheld, which is why a school can see a module it has not paid to use in
#: full and ask for more rather than waiting to be sold it.
#:
#: (key, label, requires_entitlement)
MODULES = [
    ("students", "Students Management", True),
    ("teachers", "Teachers Management", True),
    ("parents", "Parents Management", True),
    ("attendance", "Attendance Management", True),
    ("finance", "Finance", True),
    ("procurement", "Procurement", True),
    ("gradebook", "Gradebook and Assessments", True),
    ("calendar", "Calendar and Timetable", True),
    ("student_portal", "Student Portal", True),
    ("parent_portal", "Parent and Guardian Portal", True),
    # The cross-cutting services, held as one module so that a school reaches
    # bulk import everywhere at once rather than module by module. A bursar who
    # imported 900 students in January and is refused a 300-line vendor list in
    # March cannot see why the same button stopped working.
    ("platform", "Platform Services", True),
]

#: What each depth is called on the price list, per module. Three bands for
#: every module, the same three everywhere, because the whole argument for
#: selling depth rests on Plus meaning one thing.
DEPTH_BANDS = [
    ("CORE", "Core", "Every school, every tier"),
    ("PLUS", "Plus", "Standard and above"),
    ("ADVANCED", "Advanced", "Premium and above"),
]

#: Bands that carry their own key because something already refers to them by
#: name - a permission map, a runtime check, or a line on the price list that
#: is sold as itself rather than as "the Plus of that module".
#:
#: (key, label, module key, depth)
NAMED_BANDS = [
    # Core, not Plus. Loading a roll from a spreadsheet is how a school arrives
    # rather than something it grows into: a new school on the shallowest plan
    # has four hundred students in a file and no other way in, and pricing the
    # only door above them makes the cheapest plan the hardest one to start on.
    # It also sits on the onboarding checklist, so putting it out of reach put
    # a step there that most new schools could not take.
    ("bulk_import", "Bulk Data Import", "platform", "CORE"),
    # Core for the same reason as bulk import, read in the other direction. A
    # school's records are its own, and taking them out is not a feature it
    # grows into: a bursar exporting a filtered debtor list is doing ordinary
    # work, and a school that cannot get its data out is one that cannot leave.
    ("data_export", "Data Export and Reporting", "platform", "CORE"),
    # sms_alerts was removed 2026-07-12 - SMS is not part of the product.
    # Existing rows were archived (is_active=False), not deleted.
    ("email_alerts", "Email Notification Alerts", "platform", "CORE"),
]

DEPENDENCIES = {
    "procurement": ["finance"],
    "parent_portal": ["student_portal"],
}

#: Capabilities that no longer describe anything sellable. Archived rather than
#: deleted, the way sms_alerts was: entitlements, overrides and audit history
#: point at these rows.
#:
#: ``vendors`` was a module, then briefly a Core band of Procurement. Vendor
#: work is the shallow end of procurement rather than a separate purchase, and
#: a second Core band beside ``procurement_core`` was a distinction nothing
#: could act on: a school reaching one always reached the other.
#:
#: An empty band is not by itself a reason to retire one. Attendance, Gradebook
#: and the portals have empty bands because nobody has built them, and those
#: are the price list's shape for what is coming. The bands here are different:
#: their keys live in named siblings at the same depth, so they would stay
#: empty however much gets built.
RETIRED = [
    "vendors",
    # The platform module's keys all sit in named bands - email alerts at
    # Core, bulk import and data export at Plus - so its generic bands hold
    # nothing and structurally never will. Two rows at one depth of one module
    # cannot be told apart, and a school's plan page listed "Core, Core".
    "platform_core",
    "platform_plus",
    "platform_advanced",
]


class Command(BaseCommand):
    help = "Seed the capability catalogue and platform configuration definitions."

    @transaction.atomic
    def handle(self, *args, **options):
        for key, label, description, value_type, default, rules in DEFINITIONS:
            ConfigurationDefinition.objects.get_or_create(
                key=key,
                defaults={
                    "label": label, "description": description,
                    "value_type": value_type, "default_value": default,
                    "validation_rules": rules, "allowed_scopes": ["platform"],
                },
            )
        for key, label, description, value_type, default, rules in SCHOOL_SCOPED_DEFINITIONS:
            scopes = {"platform", "school"}
            if key in BRANCH_OVERRIDABLE:
                scopes.add("branch")
            row, created = ConfigurationDefinition.objects.get_or_create(
                key=key,
                defaults={
                    "label": label, "description": description,
                    "value_type": value_type, "default_value": default,
                    "validation_rules": rules,
                    "allowed_scopes": sorted(scopes),
                },
            )
            # get_or_create leaves an existing row alone, so widen it here:
            # a platform-only row refuses every school write, and a school-only
            # row every branch write.
            if not created and not scopes <= set(row.allowed_scopes or []):
                row.allowed_scopes = sorted({*(row.allowed_scopes or []), *scopes})
                row.save(update_fields=["allowed_scopes"])
        modules = {}
        for key, label, requires_entitlement in MODULES:
            modules[key], _ = Capability.objects.update_or_create(
                key=key,
                defaults={
                    "label": label, "kind": Capability.Kind.MODULE,
                    "parent": None, "depth": None,
                    "requires_entitlement": requires_entitlement, "is_active": True,
                },
            )
        bands = {}
        for module_key, module in modules.items():
            for depth_name, depth_label, description in DEPTH_BANDS:
                band_key = f"{module_key}_{depth_name.lower()}"
                bands[band_key], _ = Capability.objects.update_or_create(
                    key=band_key,
                    defaults={
                        "label": f"{module.label}: {depth_label}",
                        "description": description,
                        "kind": Capability.Kind.FEATURE,
                        "parent": module,
                        "depth": Capability.Depth[depth_name],
                        # A band holds no grant of its own; its module's depth
                        # is what opens it.
                        "requires_entitlement": False,
                        "is_active": True,
                    },
                )
        for key, label, module_key, depth_name in NAMED_BANDS:
            bands[key], _ = Capability.objects.update_or_create(
                key=key,
                defaults={
                    "label": label, "kind": Capability.Kind.FEATURE,
                    "parent": modules[module_key],
                    "depth": Capability.Depth[depth_name],
                    "requires_entitlement": False, "is_active": True,
                },
            )
        retired = Capability.objects.filter(key__in=RETIRED, is_active=True)
        retired_count = retired.update(is_active=False)
        for key, requirements in DEPENDENCIES.items():
            for required_key in requirements:
                CapabilityDependency.objects.get_or_create(
                    capability=modules[key], requires=modules[required_key]
                )
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(modules)} modules and {len(bands)} bands."
        ))
        if retired_count:
            self.stdout.write(self.style.WARNING(
                f"Archived {retired_count} retired capability row(s): "
                f"{', '.join(RETIRED)}."
            ))
