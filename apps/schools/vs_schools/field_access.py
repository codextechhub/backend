"""School fields a role may restrict, on both sides of the platform boundary.

``school.profile`` and ``school.branches`` are the school's own surfaces: a
school's choices govern what its own roles read in the school app. Platform
staff keep using the platform permission keys and serializers that administer
every tenant, so a tenant's switch never reaches CodeX's screens. ``platform.schools``
is the opposite case: CodeX's own switches over what CodeX's roles read and
change about a customer school.

Address and registration number belong to the school's own profile form, so
each receives independent Read and Write control. The declarations are open by
default to preserve the screens existing roles already use.

``school.branches`` carries a branch's address, email and phone on the school's
branches screen. CodeX alone edits a branch, so none of these fields is writable from
the school app: the switch governs who reads them. The branch name is the label
that identifies the branch everywhere and has no switch.

``platform.schools`` holds the commercial terms CodeX agrees with a school and
the billing contact it invoices. The catalogue price belongs to a plan, not to
any one school, and has its own switch from the price agreed with a school. The
billing email and phone are required to register a school, so the Write switch
governs corrections to an existing school rather than the act of creating one.
The package plan switch also governs the depth each module of a school's own
package reaches; the plan catalogue (``PackagePlanSerializer``) describes a
tier on offer rather than any school's choice, so its depths are not hidden by
it. Package notes are recorded by CodeX and no endpoint writes them, so they carry a
Read switch only. An uplift is a time-limited exception that gives one school
deeper reach into one module than its plan pays for.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"
_PLATFORM = "PLATFORM"

_SURFACE = "schools.vs_schools.serializers."


def register_audit_words():
    """Hold the values an audit snapshot repeats to the switch that governs them.

    The audit rows written when a plan's price, a school or a branch changes
    keep a before-and-after copy of the record. A reader who cannot read a
    field must not recover it from the trail, so each rule names the keys of
    that copy and the registry key that decides who may read them.

    The agreement CodeX makes with a school reaches the trail through the
    capability changes that apply it, which keep the old and new state of what
    they changed in ``metadata["before"]`` and ``metadata["after"]``. A
    subscription's start and expiry are the ``starts_at`` and ``ends_at`` of
    those states, the depth a module reaches is the plan's package depth, and
    an uplift is a depth grant of its own whose whole state is the agreement.
    A withdrawal is filed under the module (``Capability``) rather than under
    the entitlement or grant it removed. The agreed price, minimum billable
    students and package notes are stored on the school's package setup, which
    writes no audit row of its own, so no row repeats them.
    """
    from vs_audit.protected_words import ProtectedWords, register_protected_words
    rows = (
        ("schools.plan_price", "platform.schools.catalogue_price_per_student",
         "PackagePlan", ("UPDATE",), ("price_per_student",)),
        ("schools.billing_email", "platform.schools.billing_email",
         "School", ("CREATE", "UPDATE"), ("email",)),
        ("schools.billing_phone", "platform.schools.billing_phone",
         "School", ("CREATE", "UPDATE"), ("phone",)),
        ("schools.profile_address", "school.profile.address",
         "School", ("CREATE", "UPDATE"), ("address",)),
        ("schools.profile_registration_id", "school.profile.registration_id",
         "School", ("CREATE", "UPDATE"), ("registration_id",)),
        ("schools.branch_address", "school.branches.address",
         "Branch", ("CREATE", "UPDATE"), ("address",)),
        ("schools.branch_email", "school.branches.email",
         "Branch", ("CREATE", "UPDATE"), ("email",)),
    )
    for name, field_key, entity_type, actions, keys in rows:
        register_protected_words(name, ProtectedWords(
            field_key=field_key, entity_type=entity_type,
            action_types=frozenset(actions), snapshot_keys=frozenset(keys),
        ))

    entitlement = "CapabilityEntitlement"
    grant = "CapabilityDepthGrant"
    module = "Capability"
    entitlement_cleared = (("config_action", "config.entitlement.cleared"),)
    grant_cleared = (("config_action", "config.depth_grant.cleared"),)
    agreement = (
        ("subscription_starts", "platform.schools.subscription_starts_at",
         entitlement, (), ("before.starts_at", "after.starts_at")),
        ("subscription_starts_withdrawn", "platform.schools.subscription_starts_at",
         module, entitlement_cleared, ("before.starts_at",)),
        ("subscription_expires", "platform.schools.subscription_expires_at",
         entitlement, (), ("before.ends_at", "after.ends_at")),
        ("subscription_expires_withdrawn", "platform.schools.subscription_expires_at",
         module, entitlement_cleared, ("before.ends_at",)),
        ("package_depth", "platform.schools.package_plan",
         entitlement, (), ("before.depth", "after.depth")),
        ("uplift", "platform.schools.uplift", grant, (), ("before", "after")),
        ("uplift_withdrawn", "platform.schools.uplift",
         module, grant_cleared, ("before",)),
    )
    for name, field_key, entity_type, match, keys in agreement:
        register_protected_words(f"schools.agreement.{name}", ProtectedWords(
            field_key=field_key, entity_type=entity_type,
            action_types=frozenset({"CONFIG_CHANGED"}),
            metadata_keys=frozenset({"before", "after"}), metadata_match=match,
            metadata_key="", metadata_hide=frozenset(keys),
        ))


def register():
    """Publish the school-owned and school-administering fields to the registry."""
    register_fields(
        "school", "settings",
        surfaces=(
            "schools.vs_academics.views.settings.AcademicRulesView",
            "schools.vs_calendar.views.settings.CalendarRulesView",
            "schools.vs_students.views.records.EnrolmentRulesView",
            "schools.vs_students.views.records.AdmissionRulesView",
            "schools.vs_students.views.records.AdmissionPolicyView",
            "schools.vs_students.views.guardians.GuardianRulesView",
            "schools.vs_students.views.promotion.PromotionRulesView",
            "schools.vs_staff.views.settings.StaffRulesView",
            "schools.vs_staff.views.settings.StaffNumberPolicyView",
            "schools.vs_schools.views.display.SchoolDisplaySettingsView",
            "schools.vs_schools.views.settings.SchoolSecuritySettingsView",
            "schools.vs_schools.views.settings.SchoolPayrollScopeView",
            "schools.vs_attendance.views.settings.SettingsView",
            "schools.vs_attendance.serializers.ReasonCodeSerializer",
        ),
        fields=(
            FieldSpec("term_word", "What the school calls a term", group="Academics", scope=_TENANT, read_always=True),
            FieldSpec("term_names", "Names given to a new year's terms", group="Academics", scope=_TENANT),
            FieldSpec("default_arms", "Arms new classes are generated with", group="Academics", scope=_TENANT),
            FieldSpec("teaching_days", "Teaching days", group="Calendar", scope=_TENANT, read_always=True),
            FieldSpec("week_starts_on", "Week starts on", group="Calendar", scope=_TENANT, read_always=True),
            FieldSpec("closes_school_by_type", "Event types that close the school", group="Calendar", scope=_TENANT),
            FieldSpec("room_required_to_publish", "Room required to publish", group="Calendar", scope=_TENANT),
            FieldSpec("teacher_duty_match", "Teacher duty matching", group="Calendar", scope=_TENANT),
            FieldSpec("invigilator_roles", "Roles that may invigilate", group="Calendar", scope=_TENANT),
            FieldSpec("default_period_minutes", "Default period length", group="Calendar", scope=_TENANT),
            FieldSpec("min_age_years", "Minimum age", group="Enrolment", scope=_TENANT),
            FieldSpec("max_age_years", "Maximum age", group="Enrolment", scope=_TENANT),
            FieldSpec("enrolment_required_documents", "Documents required to enrol", group="Enrolment", scope=_TENANT),
            FieldSpec("required_fields", "Fields required to enrol", group="Enrolment", scope=_TENANT),
            FieldSpec("enrolment_capacity_mode", "Class capacity mode", group="Enrolment", scope=_TENANT),
            FieldSpec("default_capacity", "Default class capacity", group="Enrolment", scope=_TENANT),
            FieldSpec("min_per_student", "Guardians required per student", group="Guardians", scope=_TENANT),
            FieldSpec("email_required", "Guardian email required", group="Guardians", scope=_TENANT),
            FieldSpec("matching", "Guardian matching", group="Guardians", scope=_TENANT),
            FieldSpec("extra_relationships", "Relationships", group="Guardians", scope=_TENANT,
                      api_names=("extra_relationships", "relationships")),
            FieldSpec("parent_app_auto_access", "Automatic parent app access", group="Guardians", scope=_TENANT),
            FieldSpec("stages", "Admission stages", group="Admissions", scope=_TENANT),
            FieldSpec("required_documents_to_confirm", "Documents to confirm", group="Admissions", scope=_TENANT),
            FieldSpec("promotion_suspended", "Promotion of suspended students", group="Promotion", scope=_TENANT),
            FieldSpec("promotion_not_placed", "Promotion of students not placed", group="Promotion", scope=_TENANT),
            FieldSpec("promotion_arms", "Promotion arms", group="Promotion", scope=_TENANT),
            FieldSpec("promotion_capacity_mode", "Promotion capacity mode", group="Promotion", scope=_TENANT),
            FieldSpec("promotion_pass_mark", "Promotion pass mark", group="Promotion", scope=_TENANT),
            FieldSpec("admission_number_required", "Admission number required", group="Admission numbers", scope=_TENANT),
            FieldSpec("admission_number_pattern", "Admission number pattern", group="Admission numbers", scope=_TENANT),
            FieldSpec("admission_number_hint", "Admission number hint", group="Admission numbers", scope=_TENANT),
            FieldSpec("admission_number_auto_issue", "Issue admission numbers automatically", group="Admission numbers", scope=_TENANT),
            FieldSpec("admission_number_source", "Admission number rule source", group="Admission numbers", scope=_TENANT, writable=False),
            FieldSpec("admission_number_suggestion", "Next admission number", group="Admission numbers", scope=_TENANT, writable=False),
            FieldSpec("starting_role", "Starting role for new staff", group="Staff", scope=_TENANT,
                      sensitive=True, read_by=("school_admin",)),
            FieldSpec("staff_required_documents", "Documents required of staff", group="Staff", scope=_TENANT),
            FieldSpec("self_editable_fields", "Fields staff may edit themselves", group="Staff", scope=_TENANT),
            FieldSpec("hire_requires_approval", "Hire requires approval", group="Staff", scope=_TENANT),
            FieldSpec("leave_allowances", "Leave allowances", group="Leave", scope=_TENANT),
            FieldSpec("leave_groups", "Leave groups", group="Leave", scope=_TENANT),
            FieldSpec("leave_overrides", "Leave overrides", group="Leave", scope=_TENANT),
            FieldSpec("leave_working_days", "Leave working days", group="Leave", scope=_TENANT),
            FieldSpec("leave_exclude_closures", "Leave excludes closures", group="Leave", scope=_TENANT),
            FieldSpec("staff_number_required", "Staff number required", group="Staff numbers", scope=_TENANT),
            FieldSpec("staff_number_pattern", "Staff number pattern", group="Staff numbers", scope=_TENANT),
            FieldSpec("staff_number_hint", "Staff number hint", group="Staff numbers", scope=_TENANT),
            FieldSpec("staff_number_auto_issue", "Issue staff numbers automatically", group="Staff numbers", scope=_TENANT),
            FieldSpec("staff_number_source", "Staff number rule source", group="Staff numbers", scope=_TENANT, writable=False),
            FieldSpec("staff_number_suggestion", "Next staff number", group="Staff numbers", scope=_TENANT, writable=False),
            FieldSpec("timezone", "School time zone", group="Display", scope=_TENANT, read_always=True),
            FieldSpec("timezone_source", "Time zone source", group="Display", scope=_TENANT, writable=False),
            FieldSpec("date_format", "Date format", group="Display", scope=_TENANT, read_always=True),
            FieldSpec("clock", "Clock", group="Display", scope=_TENANT, read_always=True),
            FieldSpec("branch_timezone", "Branch time zone", group="Display", scope=_TENANT, read_always=True),
            FieldSpec("failed_login_threshold", "Failed logins before lock", group="Security", scope=_TENANT,
                      sensitive=True, read_by=("school_admin",)),
            FieldSpec("account_lock_minutes", "Account lock minutes", group="Security", scope=_TENANT,
                      sensitive=True, read_by=("school_admin",)),
            FieldSpec("self_reset_expiry_hours", "Self-reset link expiry", group="Security", scope=_TENANT,
                      sensitive=True, read_by=("school_admin",)),
            FieldSpec("admin_reset_expiry_hours", "Administrator reset link expiry", group="Security", scope=_TENANT,
                      sensitive=True, read_by=("school_admin",)),
            FieldSpec("invitation_expiry_days", "Invitation expiry", group="Security", scope=_TENANT,
                      sensitive=True, read_by=("school_admin",)),
            FieldSpec("proxy_idle_timeout_minutes", "Proxy idle timeout", group="Security", scope=_TENANT,
                      sensitive=True, read_by=("school_admin",)),
            FieldSpec("payroll_scope", "Payroll scope", group="Payroll", scope=_TENANT),
            FieldSpec("payroll_scope_source", "Payroll scope source", group="Payroll", scope=_TENANT, writable=False),
            FieldSpec("guardian_notices", "Guardian absence notices", group="Attendance", scope=_TENANT),
            FieldSpec("chronic_threshold", "Chronic absence threshold", group="Attendance", scope=_TENANT),
            FieldSpec("lesson_marking_levels", "Levels marking lessons", group="Attendance", scope=_TENANT),
            FieldSpec("edit_window_days", "Register edit window", group="Attendance", scope=_TENANT),
            FieldSpec("reason_code_name", "Reason code name", group="Attendance reasons", scope=_TENANT,
                      api_names=("reason_code_name", "name")),
            FieldSpec("counts_as_authorised", "Counts as authorised", group="Attendance reasons", scope=_TENANT),
            FieldSpec("requires_note", "Requires a note", group="Attendance reasons", scope=_TENANT),
            FieldSpec("retired", "Retired", group="Attendance reasons", scope=_TENANT),
            FieldSpec("used_this_term", "Used this term", group="Attendance reasons", scope=_TENANT, writable=False),
        ),
    )
    register_fields(
        "school",
        "profile",
        surfaces=(
            _SURFACE + "SchoolProfileSerializer",
            _SURFACE + "SchoolProfileUpdateSerializer",
        ),
        fields=(
            FieldSpec(
                "ownership_type", "Ownership type", group="Identity",
                scope=_TENANT, sort_order=10,
            ),
            FieldSpec(
                "registration_id", "Registration number", group="Identity",
                scope=_TENANT, sort_order=20,
            ),
            FieldSpec(
                "address", "Address", group="Contact", scope=_TENANT,
                sort_order=10,
            ),
            FieldSpec(
                "website", "Website", group="Contact", scope=_TENANT,
                sort_order=20,
            ),
            FieldSpec(
                "motto", "Motto", group="Identity", scope=_TENANT,
                sort_order=30,
            ),
            FieldSpec(
                "term_structure", "Term structure", group="Academics",
                scope=_TENANT, sort_order=10, read_always=True,
            ),
            FieldSpec(
                "currency", "Currency", group="Display",
                scope=_TENANT, sort_order=10, read_always=True,
            ),
        ),
    )
    register_fields(
        "school",
        "branches",
        surfaces=(_SURFACE + "SchoolBranchSerializer",),
        fields=(
            FieldSpec(
                "address", "Branch address", group="Contact", scope=_TENANT,
                sort_order=10, writable=False,
                description="Set by CodeX when the branch is provisioned or corrected.",
            ),
            FieldSpec(
                "email", "Branch email", group="Contact", scope=_TENANT,
                sort_order=20, writable=False,
                description="Set by CodeX when the branch is provisioned or corrected.",
            ),
            FieldSpec(
                "phone", "Branch phone", group="Contact", scope=_TENANT,
                sort_order=30, writable=False,
                description=(
                    "Set by CodeX when the branch is provisioned or corrected. It is the "
                    "number the branch's payment terminal notifies first; the school adds "
                    "other numbers to the terminal itself."
                ),
            ),
        ),
    )
    register_fields(
        "platform",
        "schools",
        surfaces=(
            _SURFACE + "PackagePlanSerializer",
            _SURFACE + "PackagePlanPriceSerializer",
            _SURFACE + "ChangeSchoolPlanSerializer",
            _SURFACE + "SchoolPackageSetupWriteSerializer",
            _SURFACE + "SchoolPackageSetupReadSerializer",
            _SURFACE + "SchoolPlanSummarySerializer",
            _SURFACE + "SchoolPlanModuleSerializer",
            _SURFACE + "SchoolPlanOverviewSerializer",
            _SURFACE + "SchoolListSerializer",
            _SURFACE + "SchoolDetailSerializer",
            _SURFACE + "SchoolCreateSerializer",
            _SURFACE + "SchoolUpdateSerializer",
        ),
        fields=(
            FieldSpec(
                "catalogue_price_per_student", "Catalogue price per student",
                group="Subscription pricing", scope=_PLATFORM, sort_order=10,
                api_names=("price_per_student",),
                description="The rate a plan charges every new agreement, in minor units.",
            ),
            FieldSpec(
                "agreed_price_per_student", "Agreed price per student",
                group="Subscription pricing", scope=_PLATFORM, sort_order=20,
                description="The rate agreed with one school, in minor units.",
            ),
            FieldSpec(
                "minimum_billable_students", "Minimum billable students",
                group="Subscription pricing", scope=_PLATFORM, sort_order=30,
            ),
            FieldSpec(
                "package_plan", "Package plan", group="Plan and term",
                scope=_PLATFORM, sort_order=10,
                description="The plan a school is on, and the depth it reaches from it.",
            ),
            FieldSpec(
                "subscription_starts_at", "Subscription start date",
                group="Plan and term", scope=_PLATFORM, sort_order=20,
            ),
            FieldSpec(
                "subscription_expires_at", "Subscription expiry date",
                group="Plan and term", scope=_PLATFORM, sort_order=30,
            ),
            FieldSpec(
                "uplift", "Module depth uplifts", group="Plan and term",
                scope=_PLATFORM, sort_order=40,
                description=(
                    "Deeper reach into one module than the plan pays for, "
                    "given to one school for a while."
                ),
            ),
            FieldSpec(
                "notes", "Package notes", group="Plan and term", scope=_PLATFORM,
                sort_order=50, writable=False,
                description="Operator notes recorded on the school's package.",
            ),
            FieldSpec(
                "billing_email", "School billing email", group="Billing contact",
                scope=_PLATFORM, sort_order=10, api_names=("email",),
                open_on_create=True,
                description="Where CodeX sends the school's invoices.",
            ),
            FieldSpec(
                "billing_phone", "School billing phone", group="Billing contact",
                scope=_PLATFORM, sort_order=20, api_names=("phone",),
                open_on_create=True,
            ),
        ),
    )
