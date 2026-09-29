"""Names this module's models, services, views, seeders and tests agree on.

One place, because a key a view demands and a seeder never registers fails as a
403 nobody can act on rather than as an error anybody can see.

FRD M12 v2.1, sections 5, 7 and 8.
"""
from __future__ import annotations

from django.db import models

# ── Permission keys ────────────────────────────────────────────────────────
# All seeded by core.seed_school_permissions. The resource is ``teachers`` and
# not ``staff``: the key is a primary key that four tables point at and that
# school-fe checks by name, so it stays as it is and the description changed
# instead. The account keys belong to the same module and were seeded, granted
# and grouped long before any endpoint declared them.
PERM_VIEW = "school.teachers.view"
PERM_CREATE = "school.teachers.create"
PERM_UPDATE = "school.teachers.update"
PERM_TRANSITION = "school.teachers.transition"
PERM_ASSIGN = "school.teachers.assign"

#: Loading a school's existing staff from a spreadsheet.
#:
#: Its own key, and SENSITIVE, exactly as ``school.students.import`` is: one
#: upload creates accounts, role grants and invitations for everybody in the
#: file, which is not the same act as adding one person through the form.
#:
#: The resource is ``staff`` where the register keys above say ``teachers``.
#: Those stay as they are because ``Permission.key`` is a primary key that four
#: tables point at and that school-fe checks by name; a key minted new is free
#: to say what it means.
PERM_IMPORT = "school.staff.import"

#: Qualifications, certificates and documents. Separate from the register keys
#: because reading a staff directory and reading somebody's certificates are
#: sold at different depths, and one key cannot answer for both.
PERM_RECORDS_VIEW = "school.staff_records.view"
PERM_RECORDS_UPDATE = "school.staff_records.update"

PERM_LEAVE_APPLY = "school.leave.apply"
PERM_LEAVE_VIEW = "school.leave.view"
PERM_LEAVE_UPDATE = "school.leave.update"
PERM_LEAVE_CANCEL = "school.leave.cancel"

#: The school's own organogram: units, posts, appointments and dotted lines.
#:
#: Its own resource rather than more verbs on the register, because the chart
#: is read by every member of staff while the register's history, dates and
#: counts are not. ``assign`` is appointing somebody to a post and ending that
#: appointment, which is a different act from drawing the post.
PERM_ORG_VIEW = "school.organogram.view"
PERM_ORG_CREATE = "school.organogram.create"
PERM_ORG_UPDATE = "school.organogram.update"
PERM_ORG_DELETE = "school.organogram.delete"
PERM_ORG_ASSIGN = "school.organogram.assign"

PERM_ACCOUNT_UPDATE = "school.administrators.update"
PERM_ACCOUNT_SUSPEND = "school.administrators.suspend"
PERM_ACCOUNT_REACTIVATE = "school.administrators.reactivate"

# Belongs to vs_rbac and to vs_academics respectively. Used here, never
# re-registered: FRD v2.1 section 8.1.
PERM_ROLES_ASSIGN = "school.roles.assign"
PERM_OVERRIDES_VIEW = "school.user_overrides.view"
PERM_CLASS_VIEW = "academics.classes.view"
PERM_SUBJECT_VIEW = "academics.subject.view"

#: Changing the school's own rules for its staff. The same key every school
#: settings screen writes under; reading the rules needs only the register's
#: view key, because the Add form and the leave form render from them.
PERM_SETTINGS_UPDATE = "school.settings.update"


class EmploymentStatus(models.TextChoices):
    """Does this person still work here.

    Not a statement about signing in. That question is ``User.status``' and is
    enforced by two allow-lists this column cannot reach. LOCKED is absent on
    purpose: it is a security lockout cleared by a password reset, and a teacher
    who mistyped her password three times on a Tuesday is employed, at work, and
    standing in front of her class.
    """

    #: Added at a school that approves each hire before inviting it. The
    #: account exists and holds its starting role, and no invitation has been
    #: sent. Only the approval decides where it goes next: Invited when it is
    #: approved, Terminated when it is not (``hire_approval.py``).
    PENDING_APPROVAL = "PENDING_APPROVAL", "Awaiting approval"
    #: Imported while the school is still being set up. The account and the
    #: starting role exist and no invitation has been sent: every one goes out
    #: together when the school goes live (``services/setup_invitations.py``).
    AWAITING_GO_LIVE = "AWAITING_GO_LIVE", "Invited at go-live"
    INVITED = "INVITED", "Invited"
    ACTIVE = "ACTIVE", "Active"
    #: **Derived, never set.** Nobody moves a person here: a member of staff is
    #: on leave exactly while an approved leave request covers today, and the
    #: serializer computes that at read time. The value stays in the vocabulary
    #: because history rows written before the rule changed still name it, and
    #: because the directory filter and the header count still speak it.
    ON_LEAVE = "ON_LEAVE", "On Leave"
    SUSPENDED = "SUSPENDED", "Suspended"
    RESIGNED = "RESIGNED", "Resigned"
    TERMINATED = "TERMINATED", "Terminated"


#: The only moves an administrator may make, read as {from: (to, ...)}.
#:
#: PENDING_APPROVAL has no moves: only the hire's approval decides where it goes.
#: AWAITING_GO_LIVE has none either: going live is what moves it.
#: INVITED has none either. It leaves only when the invited person uses
#: their own link and sets a password, which is what promotes the account; an
#: administrator doing it on their behalf would move the employment status while
#: the account stayed PENDING, leaving somebody who reads Active on every screen
#: and cannot sign in.
#: ON_LEAVE is absent as a TARGET, everywhere. Going on leave is not a decision
#: an administrator takes about somebody's employment; it is what an approved
#: leave request means while its dates are running. Offering the move as well
#: would be a second way in that nothing takes back out: approval is an event
#: and code can hang off it, but a leave ENDING is not one, and there is no
#: scheduler in this repository to notice. Somebody set On Leave by hand on 17
#: August is still On Leave the following March.
#:
#: It survives as a SOURCE so a row written under the old rule can be moved off
#: it. The migration that came with this change empties that case, so the escape
#: is a belt rather than a path anybody walks.
EMPLOYMENT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    EmploymentStatus.PENDING_APPROVAL: (),
    EmploymentStatus.AWAITING_GO_LIVE: (),
    EmploymentStatus.INVITED: (),
    EmploymentStatus.ACTIVE: (
        EmploymentStatus.SUSPENDED,
        EmploymentStatus.RESIGNED,
        EmploymentStatus.TERMINATED,
    ),
    EmploymentStatus.ON_LEAVE: (
        EmploymentStatus.ACTIVE,
        EmploymentStatus.SUSPENDED,
        EmploymentStatus.RESIGNED,
        EmploymentStatus.TERMINATED,
    ),
    EmploymentStatus.SUSPENDED: (
        EmploymentStatus.ACTIVE,
        EmploymentStatus.RESIGNED,
        EmploymentStatus.TERMINATED,
    ),
    EmploymentStatus.RESIGNED: (),
    EmploymentStatus.TERMINATED: (),
}

#: The two statuses that mean somebody no longer works here.
#:
#: Written out in three places before this existed, which is two places for it
#: to fall out of step with itself. "Off roll" is not "cannot sign in" and not
#: "absent": a suspended teacher is still employed and still on the roll, and
#: somebody on approved leave is at their post next month.
OFF_ROLL_STATUSES = frozenset({
    EmploymentStatus.RESIGNED,
    EmploymentStatus.TERMINATED,
})

#: Account statuses that mean the invitation has not been accepted yet.
#:
#: Read once, when an employment record is first written, to decide where its
#: history starts. Everything outside this set belongs to somebody who did
#: accept, whatever has become of their login since: a suspension, a security
#: lockout and a closed account are all states reached after signing in, and the
#: employment record beside them says Active while the account says the rest.
#: Keeping the two apart is the rule this module is built around.
UNACCEPTED_ACCOUNT_STATUSES = frozenset({
    "DRAFT", "PENDING_APPROVAL", "PENDING", "REJECTED",
})

#: Transitions that must say why. Suspending, resigning and terminating are the
#: three a school is asked to account for later.
REASON_REQUIRED_FOR = frozenset({
    EmploymentStatus.SUSPENDED,
    EmploymentStatus.RESIGNED,
    EmploymentStatus.TERMINATED,
})

#: Transitions that must carry a last working day, which is copied to
#: ``StaffProfile.exit_date`` by the same service that writes the event.
LAST_WORKING_DAY_REQUIRED_FOR = frozenset({
    EmploymentStatus.RESIGNED,
    EmploymentStatus.TERMINATED,
})

#: What each transition asks the identity services to do, and nothing else does.
#:
#: A value of None means the account is left exactly as it is. ON_LEAVE is None
#: because going on leave does not close a login and a teacher on maternity
#: leave still reads her school's calendar. RESIGNED is None because the last
#: working day may be in the future and nothing in this repository runs on a
#: schedule to notice when it passes; FRD section 3.4 records that as a refusal
#: rather than a gap, and FR-002's directory warning is what a school gets.
ACCOUNT_EFFECT: dict[str, str | None] = {
    EmploymentStatus.ACTIVE: "reactivate",
    EmploymentStatus.ON_LEAVE: None,
    EmploymentStatus.SUSPENDED: "suspend",
    EmploymentStatus.RESIGNED: None,
    EmploymentStatus.TERMINATED: "deactivate",
}


class EmploymentType(models.TextChoices):
    """What kind of contract, and nothing about its terms.

    No hours, no part-time pattern, no maximum load. A contract that is wrong is
    worse than no contract and nobody has asked a school what shape theirs is;
    FRD section 14, decision 6. The first three match
    ``PlatformStaffProfile.EmploymentType`` so the two vocabularies do not
    diverge; INTERN is dropped and VOLUNTEER added, which is the Nigerian
    private-school shape rather than CodeX's.
    """

    FULL_TIME = "FULL_TIME", "Full-time"
    PART_TIME = "PART_TIME", "Part-time"
    CONTRACT = "CONTRACT", "Contract"
    VOLUNTEER = "VOLUNTEER", "Volunteer"


class DocumentType(models.TextChoices):
    """A closed list, because it is what the filter and the grouping read.

    A degree certificate and a professional one are separate values: a school
    looking for somebody's teaching qualification is not looking for their ICAN
    membership. OTHER carries the long tail.
    """

    CV = "CV", "CV"
    DEGREE_CERTIFICATE = "DEGREE_CERTIFICATE", "Degree certificate"
    PROFESSIONAL_CERTIFICATE = "PROFESSIONAL_CERTIFICATE", "Professional certificate"
    IDENTIFICATION = "IDENTIFICATION", "National ID"
    OTHER = "OTHER", "Other"


class LeaveType(models.TextChoices):
    """The seven Nigerian schools use. Closed, because the report groups by it."""

    ANNUAL = "ANNUAL", "Annual"
    SICK = "SICK", "Sick"
    MATERNITY = "MATERNITY", "Maternity"
    PATERNITY = "PATERNITY", "Paternity"
    STUDY = "STUDY", "Study"
    COMPASSIONATE = "COMPASSIONATE", "Compassionate"
    OTHER = "OTHER", "Other"


class LeaveStatus(models.TextChoices):
    """Where a request has got to.

    ``Completed`` is not here. It is derived from the end date at read time, and
    a derived value stored beside the value it derives from is a second thing
    that can be wrong.
    """

    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


#: Statuses whose dates count against a person when a new request overlaps.
#: A rejected or cancelled request is not an absence and must not warn.
LEAVE_LIVE_STATUSES = frozenset({LeaveStatus.PENDING, LeaveStatus.APPROVED})


class TeachingPart(models.TextChoices):
    """Whether this person carries the subject in this class, or helps with it.

    The main teacher is the one who enters the subject's results for that class.
    At most one per (session, class, subject), enforced by a partial unique
    constraint; the people assisting are unbounded. A class subject with people
    assisting and no main teacher is being taught and unaccounted for, which is
    a different problem from nobody teaching it, and FR-017 counts the two
    separately.

    The stored values stay LEAD and ASSISTANT. Only the labels read as a school
    speaks: "lead" and "class teacher" both sounded like "the one responsible
    for this class" and were routinely read as the same designation, which they
    are not - one carries a subject, the other looks after the class itself.
    """

    LEAD = "LEAD", "Main teacher"
    ASSISTANT = "ASSISTANT", "Assisting"


# ── Workflow ───────────────────────────────────────────────────────────────
#: Leave requests: every absence is decided by this ladder.
LEAVE_DOCUMENT_TYPE = "schools.leave_request"
LEAVE_TEMPLATE_CODE = "leave-request"
LEAVE_TEMPLATE_NAME = "Leave-request approval"

#: The approver pool that decides a leave request.
#:
#: A GROUP and not a role, which is the difference worth reading. A provisioned
#: role puts a row on the school's own Roles and access screen that nobody at
#: the school created, that holds nobody, that cannot be deleted because the
#: leave ladder resolves through it, and that the screen gives no way to explain.
#: This module renders that screen, so it would have been shipping its own
#: confusion.
#:
#: A group says the same thing without inventing a role: the stage points at
#: "Leave Approvers", and who that reaches is a list the school composes from
#: its own people, its own roles or its own org seats. Changing it is one screen
#: rather than a permission-model conversation.
#:
#: Provisioning creates the group EMPTY. Until a school puts somebody in it, a
#: filed request parks rather than being approved unseen, which is the
#: seeded-blocked-not-seeded-open contract every other ladder here keeps.
LEAVE_APPROVER_GROUP_CODE = "leave-approvers"

#: New hires, at a school that approves each one before it is invited.
#:
#: The document is the staff record itself. Its approvers are a group, for the
#: reason ``LEAVE_APPROVER_GROUP_CODE`` gives, created empty so a hire filed
#: before anybody is nominated parks rather than being invited unseen.
HIRE_DOCUMENT_TYPE = "schools.staff_hire"
HIRE_TEMPLATE_CODE = "staff-hire"
HIRE_TEMPLATE_NAME = "New staff approval"
HIRE_APPROVER_GROUP_CODE = "hire-approvers"


# ── A school's own staff rules ─────────────────────────────────────────────
#: The ``vs_config`` definitions behind Settings, Staff. Declared by
#: ``migrations/0009_staff_settings.py`` and by ``seed_config_catalogue`` with
#: the same shape. Every default is the behaviour a school has before it
#: chooses, so a school that saves nothing is treated exactly as before.
CFG_NUMBER_REQUIRED = "staff.number.required"
CFG_NUMBER_PATTERN = "staff.number.pattern"
CFG_NUMBER_HINT = "staff.number.hint"
CFG_NUMBER_AUTO_ISSUE = "staff.number.auto_issue"
CFG_STARTING_ROLE = "staff.starting_role"
CFG_REQUIRED_DOCUMENTS = "staff.documents.required"
CFG_SELF_EDITABLE = "staff.self_editable_fields"
CFG_HIRE_APPROVAL = "staff.hire.requires_approval"
CFG_LEAVE_ALLOWANCES = "staff.leave.allowances"
CFG_LEAVE_WORKING_DAYS = "staff.leave.working_days"
CFG_LEAVE_EXCLUDE_CLOSURES = "staff.leave.exclude_closures"

#: The four keys of the staff-number rule, which a branch may hold as a whole.
NUMBER_POLICY_KEYS = (
    CFG_NUMBER_REQUIRED, CFG_NUMBER_PATTERN, CFG_NUMBER_HINT, CFG_NUMBER_AUTO_ISSUE,
)

#: The role new staff start with where a school has not chosen one. Matched on
#: the KEY, because the name is the school's to rename and the key is not.
DEFAULT_STARTING_ROLE_KEY = "teacher"

#: What a person may change about their own record where the school has not
#: chosen: the four personal facts nobody else is better placed to correct.
DEFAULT_SELF_EDITABLE = ("middle_name", "date_of_birth", "photo", "phone")

#: Fields nobody may change about themselves, whatever the school chooses.
#:
#: The staff ID is also a sign-in identifier. The job title, the employment
#: type, the hire and exit dates and the posting are the school's statements
#: about the job: editing your own hire date is editing your own tenure, and
#: editing your own job title is a promotion nobody gave. The email is changed
#: on an endpoint of its own. The role and anything to do with pay are not
#: fields of this record at all.
SELF_EDIT_FLOOR = (
    "staff_number", "job_title", "employment_type", "hire_date", "exit_date",
    "email", "branch",
)

#: Labels for the floor's entries that are not registered fields.
SELF_EDIT_FLOOR_LABELS = {"branch": "Posting"}

#: Monday to Friday, as ISO weekdays (Monday is 1, Sunday is 7).
DEFAULT_WORKING_DAYS = (1, 2, 3, 4, 5)

#: The most days a leave allowance may be, per type, per session.
LEAVE_ALLOWANCE_MAX = 366


# ── Organogram ─────────────────────────────────────────────────────────────
class OrgUnitKind(models.TextChoices):
    """The three tiers of a school's org chart, widest first.

    The same tiers the platform chart uses, so a person who has drawn one has
    drawn the other: a division sits at the top, a department under a division,
    and a team under a department.
    """

    DIVISION = "DIVISION", "Division"
    DEPARTMENT = "DEPARTMENT", "Department"
    TEAM = "TEAM", "Team"


#: Which tier each kind must sit under. None means it must be at the top.
ORG_UNIT_PARENT_KIND: dict[str, str | None] = {
    OrgUnitKind.DIVISION: None,
    OrgUnitKind.DEPARTMENT: OrgUnitKind.DIVISION,
    OrgUnitKind.TEAM: OrgUnitKind.DEPARTMENT,
}

#: The prefix a unit's code carries, so a code says which tier it names.
ORG_UNIT_CODE_PREFIX: dict[str, str] = {
    OrgUnitKind.DIVISION: "DV-",
    OrgUnitKind.DEPARTMENT: "DT-",
    OrgUnitKind.TEAM: "TM-",
}

#: Employment statuses that never count as holding a post.
#:
#: An invited person may be appointed ahead of their first day, so the seat is
#: reserved for them, but it reads vacant until they accept: a chart naming
#: somebody who has never signed in names somebody who may never arrive. The
#: two exit statuses are here as a belt, because leaving closes the appointment
#: in the same transaction and no open appointment of theirs should remain.
NON_HOLDING_STATUSES = frozenset({
    EmploymentStatus.PENDING_APPROVAL,
    EmploymentStatus.AWAITING_GO_LIVE,
    EmploymentStatus.INVITED,
    EmploymentStatus.RESIGNED,
    EmploymentStatus.TERMINATED,
})
