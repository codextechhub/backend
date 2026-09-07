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
PERM_MANAGE = "school.teachers.manage"
PERM_ASSIGN = "school.teachers.assign"

#: Qualifications, certificates and documents. Separate from the register keys
#: because reading a staff directory and reading somebody's certificates are
#: sold at different depths, and one key cannot answer for both.
PERM_RECORDS_VIEW = "school.staff_records.view"
PERM_RECORDS_UPDATE = "school.staff_records.update"

PERM_LEAVE_APPLY = "school.leave.apply"
PERM_LEAVE_VIEW = "school.leave.view"
PERM_LEAVE_MANAGE = "school.leave.manage"

PERM_ACCOUNT_UPDATE = "school.administrators.update"
PERM_ACCOUNT_SUSPEND = "school.administrators.suspend"
PERM_ACCOUNT_REACTIVATE = "school.administrators.reactivate"

# Belongs to vs_rbac and to vs_academics respectively. Used here, never
# re-registered: FRD v2.1 section 8.1.
PERM_ROLES_ASSIGN = "school.roles.assign"
PERM_OVERRIDES_VIEW = "school.user_overrides.view"
PERM_CLASS_VIEW = "academics.classes.view"
PERM_SUBJECT_VIEW = "academics.subject.view"


class EmploymentStatus(models.TextChoices):
    """Does this person still work here.

    Not a statement about signing in. That question is ``User.status``' and is
    enforced by two allow-lists this column cannot reach. LOCKED is absent on
    purpose: it is a security lockout cleared by a password reset, and a teacher
    who mistyped her password three times on a Tuesday is employed, at work, and
    standing in front of her class.
    """

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
#: INVITED has no entry at all. It leaves only when the invited person uses
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
    """Whether this person owns the subject in this class, or helps with it.

    At most one lead per (session, class, subject), enforced by a partial unique
    constraint. Assistants are unbounded. A pairing with assistants and no lead
    is being taught and unowned, which is a different problem from nobody
    teaching it, and FR-017 counts the two separately.
    """

    LEAD = "LEAD", "Lead"
    ASSISTANT = "ASSISTANT", "Assistant"


# ── Workflow ───────────────────────────────────────────────────────────────
#: The one approvable document type in this app.
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
