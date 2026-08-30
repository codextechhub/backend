"""Names this module's models, services, views, seeders and tests agree on.

One place, because a key a view demands and a seeder never registers fails as
a 403 nobody can act on rather than as an error anybody can see.

FRD M11 v2.4, sections 7, 8 and 11.
"""
from __future__ import annotations

from django.db import models

# ── Permission keys ────────────────────────────────────────────────────────
# The first five are seeded already (core.seed_school_permissions); the last
# two are added by the same seeder in this change. academics.classes.assign is
# M13's and is used, never re-registered: FRD v2.4 section 8.1.
PERM_VIEW = "school.students.view"
PERM_CREATE = "school.students.create"
PERM_UPDATE = "school.students.update"
PERM_MANAGE = "school.students.manage"
PERM_VIEW_SENSITIVE = "school.students.view_sensitive"
PERM_IMPORT = "school.students.import"
PERM_EXPORT = "school.students.export"

PERM_CLASS_ASSIGN = "academics.classes.assign"
PERM_CLASS_VIEW = "academics.classes.view"

# ── Configuration keys (vs_config) ─────────────────────────────────────────
# The admission-number policy is a school's own rule, so it lives in the
# platform's settings machinery and not in a column here. FRD v2.4 section 7.7.
CFG_ADM_REQUIRED = "students.admission_number.required"
CFG_ADM_PATTERN = "students.admission_number.pattern"
CFG_ADM_HINT = "students.admission_number.hint"


class StudentStatus(models.TextChoices):
    APPLICANT = "APPLICANT", "Applicant"        # started, not confirmed
    ENROLLED = "ENROLLED", "Enrolled"           # confirmed, not placed
    ACTIVE = "ACTIVE", "Active"                 # placed and attending
    SUSPENDED = "SUSPENDED", "Suspended"        # temporarily restricted
    WITHDRAWN = "WITHDRAWN", "Withdrawn"        # left the school
    GRADUATED = "GRADUATED", "Graduated"        # completed the final level
    TRANSFERRED = "TRANSFERRED", "Transferred"  # left for another school
    REJECTED = "REJECTED", "Rejected"           # application closed


#: The only transitions this module allows. FRD v2.4 FR-011.
#:
#: Three of these are worth reading twice. WITHDRAWN returns to ENROLLED and
#: not to ACTIVE, because ACTIVE means placed and attending and a readmitted
#: student has no placement yet; FR-006 carries them the rest of the way.
#: TRANSFERRED and REJECTED are terminal, like GRADUATED: a school that changes
#: its mind about a rejected application enrols the child afresh, because an
#: application closed and reopened is a different fact from one never closed.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    StudentStatus.APPLICANT: frozenset({StudentStatus.ENROLLED, StudentStatus.REJECTED}),
    StudentStatus.ENROLLED: frozenset({StudentStatus.ACTIVE, StudentStatus.WITHDRAWN}),
    StudentStatus.ACTIVE: frozenset({
        StudentStatus.SUSPENDED, StudentStatus.WITHDRAWN,
        StudentStatus.TRANSFERRED, StudentStatus.GRADUATED,
    }),
    StudentStatus.SUSPENDED: frozenset({StudentStatus.ACTIVE, StudentStatus.WITHDRAWN}),
    StudentStatus.WITHDRAWN: frozenset({StudentStatus.ENROLLED}),
    StudentStatus.GRADUATED: frozenset(),
    StudentStatus.TRANSFERRED: frozenset(),
    StudentStatus.REJECTED: frozenset(),
}

#: A student who is on the roll: countable, placeable, promotable.
ON_ROLL = frozenset({
    StudentStatus.ENROLLED, StudentStatus.ACTIVE, StudentStatus.SUSPENDED,
})

#: Leaving any of these releases the class seat. The record keeps its history;
#: the seat does not.
LEAVES_THE_ROLL = frozenset({
    StudentStatus.WITHDRAWN, StudentStatus.TRANSFERRED,
    StudentStatus.GRADUATED, StudentStatus.REJECTED,
})

#: What the directory shows unless a status filter says otherwise. A withdrawn
#: or graduated student is still a record and is still reachable by name; they
#: are simply not what "the students at this school" means.
DEFAULT_LIST_STATUSES = frozenset({
    StudentStatus.APPLICANT, StudentStatus.ENROLLED,
    StudentStatus.ACTIVE, StudentStatus.SUSPENDED,
})


class Gender(models.TextChoices):
    FEMALE = "FEMALE", "Female"
    MALE = "MALE", "Male"


class Relationship(models.TextChoices):
    """Eight, not five.

    An aunt recorded as OTHER is a contact the school cannot tell apart from a
    neighbour, and the school knew which she was when it typed her in.
    """

    MOTHER = "MOTHER", "Mother"
    FATHER = "FATHER", "Father"
    UNCLE = "UNCLE", "Uncle"
    AUNT = "AUNT", "Aunt"
    GRANDPARENT = "GRANDPARENT", "Grandparent"
    LEGAL_GUARDIAN = "LEGAL_GUARDIAN", "Legal guardian"
    SIBLING = "SIBLING", "Sibling"
    OTHER = "OTHER", "Other"


class TransferReason(models.TextChoices):
    PARENT_REQUEST = "PARENT_REQUEST", "Parent request"
    STREAM_CHANGE = "STREAM_CHANGE", "Stream change"
    CLASS_BALANCING = "CLASS_BALANCING", "Class balancing"
    BEHAVIOUR = "BEHAVIOUR", "Behaviour"
    ACADEMIC_PLACEMENT = "ACADEMIC_PLACEMENT", "Academic placement"
    OTHER = "OTHER", "Other"


class EnrolmentOutcome(models.TextChoices):
    """What happened to a placement.

    Without this a closed enrolment records that it ended and not why, and the
    profile's class-history trail cannot tell a promotion from a withdrawal.
    """

    CURRENT = "CURRENT", "Current"
    PROMOTED = "PROMOTED", "Promoted"
    REPEATED = "REPEATED", "Repeated"
    GRADUATED = "GRADUATED", "Graduated"
    TRANSFERRED = "TRANSFERRED", "Transferred"
    ENDED = "ENDED", "Ended"


class DocumentType(models.TextChoices):
    BIRTH_CERTIFICATE = "BIRTH_CERTIFICATE", "Birth certificate"
    REPORT_CARD = "REPORT_CARD", "Previous report card"
    PASSPORT_PHOTO = "PASSPORT_PHOTO", "Passport photograph"
    TRANSFER_CERTIFICATE = "TRANSFER_CERTIFICATE", "Transfer certificate"
    IMMUNISATION = "IMMUNISATION", "Immunisation record"


#: Which document types a school is prompted for. A prompt, never a gate: a
#: school registering a child on the day they arrive rarely has the birth
#: certificate in hand, and a rule that refused the enrolment would be worked
#: around with a blank file. FRD v2.4 FR-015 rule 4.
REQUIRED_DOCUMENTS = frozenset({
    DocumentType.BIRTH_CERTIFICATE, DocumentType.PASSPORT_PHOTO,
})


class PromotionOutcome(models.TextChoices):
    """What the promotion run does with one student. FRD v2.4 FR-010 rule 1."""

    PROMOTE = "PROMOTE", "Promote"
    REPEAT = "REPEAT", "Repeat"
    GRADUATE = "GRADUATE", "Graduate"
    HOLD = "HOLD", "Hold"


#: Why a student is on the promotion exception list. A fixed vocabulary,
#: because the screen prints the sentence and a free-text reason drifts.
EXC_TERMINAL_LEVEL = "TERMINAL_LEVEL"
EXC_NO_CLASS_AT_NEXT_LEVEL = "NO_CLASS_AT_NEXT_LEVEL"
EXC_STUDENT_SUSPENDED = "STUDENT_SUSPENDED"
EXC_NO_CLASS_ASSIGNED = "NO_CLASS_ASSIGNED"

#: The palette's result cap and its minimum query length. A palette that
#: paginates is a list, and this is not one.
SEARCH_LIMIT = 8
SEARCH_MIN_CHARS = 2

#: The ceiling on one bulk action. A bulk route with no ceiling is a way to
#: hold a worker open for a minute.
BULK_MAX = 200
