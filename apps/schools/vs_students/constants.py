"""Names this module's models, services, views, seeders and tests agree on.

One place, because a key a view demands and a seeder never registers fails as
a 403 nobody can act on rather than as an error anybody can see.

FRD M11 v2.4, sections 7, 8 and 11.
"""
from __future__ import annotations

from django.db import models

# ── Permission keys ────────────────────────────────────────────────────────
# All seeded by core.seed_school_permissions. academics.classes.assign belongs
# to ``vs_academics`` and is used here, never re-registered: FRD v2.4 sec 8.1.
PERM_VIEW = "school.students.view"
PERM_CREATE = "school.students.create"
PERM_UPDATE = "school.students.update"
PERM_TRANSITION = "school.students.transition"
PERM_TRANSFER = "school.students.transfer"
PERM_SUSPEND = "school.students.suspend"
PERM_REACTIVATE = "school.students.reactivate"
PERM_IMPORT = "school.students.import"
PERM_EXPORT = "school.students.export"

#: Promotion is a bulk act on the whole roll. It has its own permission,
#: the key that also transfers one child between branches, so the two could
#: never be sold at different depths.
PERM_PROMOTE = "school.students.promote"

#: A school's own settings. The enrolment rules are written under it rather
#: than a student key, because they are the school's settings screen and not a
#: student record. Seeded by ``vs_schools``, which owns the settings screens.
PERM_SETTINGS_UPDATE = "school.settings.update"

PERM_CLASS_ASSIGN = "academics.classes.assign"
PERM_CLASS_VIEW = "academics.classes.view"

# No key names a restricted field of a student. Blood group, allergies,
# conditions and the enrolment date are registered fields of
# ``school.students`` (``schools/vs_students/field_access.py``), and a role's
# own Read and Write switches decide each of them on every path: the enrolment
# form, the edit route and the spreadsheet import alike.

# ── Configuration keys (vs_config) ─────────────────────────────────────────
# The admission-number policy is a school's own rule, so it lives in the
# platform's settings machinery and not in a column here. FRD v2.4 section 7.7.
# A branch may hold its own rule, which replaces the school's for its students.
CFG_ADM_REQUIRED = "students.admission_number.required"
CFG_ADM_PATTERN = "students.admission_number.pattern"
CFG_ADM_HINT = "students.admission_number.hint"
CFG_ADM_AUTO_ISSUE = "students.admission_number.auto_issue"

#: The four keys that make up one admission-number rule. A branch that has its
#: own rule holds all four, so the rule is read as a unit (services/policy.py).
ADMISSION_POLICY_KEYS = (
    CFG_ADM_REQUIRED, CFG_ADM_PATTERN, CFG_ADM_HINT, CFG_ADM_AUTO_ISSUE,
)

# The school's enrolment rules (services/rules.py). Each default reproduces the
# behaviour a school had before it could choose, so a school that has set
# nothing is refused and allowed exactly what every school always was.
CFG_AGE_MIN = "students.age.min_years"
CFG_AGE_MAX = "students.age.max_years"
CFG_REQUIRED_DOCUMENTS = "students.documents.required"
CFG_REQUIRED_FIELDS = "students.enrolment.required_fields"
CFG_CAPACITY_MODE = "students.capacity.mode"
CFG_CAPACITY_DEFAULT = "students.capacity.default"

#: The bounds a school's age rule may be set within.
AGE_RULE_FLOOR = 0
AGE_RULE_CEILING = 99

#: The most seats a school may give as its default class size.
DEFAULT_CAPACITY_MIN = 1
DEFAULT_CAPACITY_MAX = 500

# The school's guardian rules (services/guardian_rules.py). As with the
# enrolment rules, each default is the behaviour every school had before it
# could choose: one guardian, no email demanded, email then phone, and the
# eight fixed relationships.
CFG_GUARDIAN_MINIMUM = "guardians.min_per_student"
CFG_GUARDIAN_EMAIL_REQUIRED = "guardians.email_required"
CFG_GUARDIAN_MATCHING = "guardians.matching"
CFG_GUARDIAN_EXTRA_RELATIONSHIPS = "guardians.relationships.extra"

#: How many guardians a school may ask for on every child.
GUARDIAN_MINIMUM_FLOOR = 1
GUARDIAN_MINIMUM_CEILING = 4

#: How many relationships of its own a school may add, and how long each may be.
#: The length is ``StudentGuardian.relationship_detail``'s.
EXTRA_RELATIONSHIPS_MAX = 10
EXTRA_RELATIONSHIP_MAX_LENGTH = 30

# The school's applicant rules (services/admission.py). The admission stages
# are rows (``AdmissionStage``); the documents an applicant must hold before
# being confirmed are a setting, whose default of none is the behaviour every
# school had before it could choose.
CFG_CONFIRM_DOCUMENTS = "applicants.documents.required_to_confirm"

#: How many admission stages a school may name, and how long each name may be.
#: The length is ``AdmissionStage.name``'s.
ADMISSION_STAGES_MAX = 12
ADMISSION_STAGE_NAME_MAX_LENGTH = 40

#: How long an offer may be left open, in days.
OFFER_DAYS_MIN = 1
OFFER_DAYS_MAX = 365

# The school's promotion rules (services/promotion_rules.py). Each default is
# the behaviour every school had before it could choose: suspended and
# unplaced pupils are held, each arm moves up whole, and the run follows the
# enrolment capacity rule.
CFG_PROMOTION_SUSPENDED = "students.promotion.suspended"
CFG_PROMOTION_NOT_PLACED = "students.promotion.not_placed"
CFG_PROMOTION_ARMS = "students.promotion.arms"
CFG_PROMOTION_CAPACITY_MODE = "students.promotion.capacity_mode"


class PromotionSuspended(models.TextChoices):
    """What the end-of-year promotion does with a suspended pupil.

    HOLD lists them as an exception and leaves them where they are. PROMOTE
    moves them up with their year group, still suspended: the status is
    theirs, not the promotion's, and the review screen can still hold or
    repeat any one of them.
    """

    HOLD = "HOLD", "Hold them where they are"
    PROMOTE = "PROMOTE", "Move them up, still suspended"


class PromotionNotPlaced(models.TextChoices):
    """What the promotion does with a pupil who is confirmed but not placed.

    Such a pupil is ENROLLED yet holds a class in the year being left. HOLD
    leaves them there for a person to decide; PROMOTE moves them up with the
    class they hold and makes them ACTIVE, as giving them a class by hand
    does.
    """

    HOLD = "HOLD", "Hold them where they are"
    PROMOTE = "PROMOTE", "Move them up with their class"


class PromotionArms(models.TextChoices):
    """Which of next year's classes a promoted pupil joins.

    SAME_ARM keeps an arm together (JSS1 B to JSS2 B, or the first class at
    the level when there is no B). SPREAD shares the pupils moving into a
    level evenly across that level's classes, emptiest first.
    """

    SAME_ARM = "SAME_ARM", "Keep each arm together"
    SPREAD = "SPREAD", "Share pupils evenly across the classes"


class PromotionCapacityMode(models.TextChoices):
    """What the promotion does when it would fill a class past its capacity.

    FOLLOW_ENROLMENT applies the school's enrolment rule
    (``students.capacity.mode``); the other three mean what ``CapacityMode``'s
    do, for the promotion alone.
    """

    FOLLOW_ENROLMENT = "FOLLOW_ENROLMENT", "Same as the enrolment rule"
    WARN = "WARN", "Warn, and let staff go ahead"
    HARD = "HARD", "Refuse, with no override"
    OFF = "OFF", "Do not check"


class GuardianMatching(models.TextChoices):
    """How a guardian typed in is recognised as one the school already holds.

    EMAIL_THEN_PHONE is the behaviour every school had before it could choose:
    siblings whose parent is typed in with only a phone number still join one
    household. EMAIL_ONLY is for a school where families share landlines, so a
    shared number is two households and never one.
    """

    EMAIL_THEN_PHONE = "EMAIL_THEN_PHONE", "Email, then phone"
    EMAIL_ONLY = "EMAIL_ONLY", "Email only"


class CapacityMode(models.TextChoices):
    """What a full class does when one more child is placed in it.

    WARN is the behaviour every school had before it could choose: refused
    until the person placing the child says they mean it. HARD refuses with no
    way past, for a school whose rooms genuinely hold no more. OFF checks
    nothing, for a school that sets capacities as a planning figure only.
    """

    WARN = "WARN", "Warn, and let staff go ahead"
    HARD = "HARD", "Refuse, with no override"
    OFF = "OFF", "Do not check"


#: The enrolment fields a school may make required, each with the label the
#: enrolment form gives it, so Settings and the form name a detail the same way. Every one is optional
#: on ``EnrolmentWriteSerializer``; a test holds the two together. Allergies and
#: conditions are absent on purpose: for most children the true answer is
#: "none", and a required box teaches staff to type "none" into it.
REQUIRABLE_FIELDS = {
    "nationality": "Nationality",
    "state_of_origin": "State of origin",
    "address": "Home address",
    "phone": "Student phone",
    "email": "Student email",
    "previous_school": "Previous school",
    "emergency_contact_name": "Emergency contact",
    "emergency_contact_phone": "Emergency phone",
    "blood_group": "Blood group",
    "middle_name": "Middle name",
}


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

    A school may add relationships of its own (``guardians.relationships.extra``).
    Those are stored as OTHER with the school's label in
    ``StudentGuardian.relationship_detail``, so the fixed codes stay the whole
    vocabulary every report and client can rely on.
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


#: Which document types a school is prompted for until it chooses its own list
#: (``students.documents.required``, read through ``services/rules.py``). A
#: prompt, never a gate, whatever the list holds: a school registering a child
#: on the day they arrive rarely has the birth certificate in hand, and a rule
#: that refused the enrolment would be worked around with a blank file. FRD
#: v2.4 FR-015 rule 4.
#:
#: A photograph is not among the defaults, here or on a guardian. A school photographs
#: its intake on a day it chooses, not at the desk while a parent waits, so a
#: record marked incomplete for a missing picture is marked incomplete for
#: every child on their first day - which teaches everybody to ignore the mark
#: on the row that is genuinely missing a birth certificate.
REQUIRED_DOCUMENTS = frozenset({
    DocumentType.BIRTH_CERTIFICATE,
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
#: A level with no promotion target AND no terminal flag - nobody has wired it.
#: Distinct from TERMINAL_LEVEL on purpose: see Level.next_level's comment and
#: FRD v2.7 FR-005. Treating the two alike graduates a year group by accident.
EXC_LEVEL_NOT_WIRED = "LEVEL_NOT_WIRED"
EXC_NO_CLASS_AT_NEXT_LEVEL = "NO_CLASS_AT_NEXT_LEVEL"
EXC_STUDENT_SUSPENDED = "STUDENT_SUSPENDED"
EXC_NO_CLASS_ASSIGNED = "NO_CLASS_ASSIGNED"
EXC_NO_CLASS_TO_REPEAT = "NO_CLASS_TO_REPEAT"

#: The palette's result cap and its minimum query length. A palette that
#: paginates is a list, and this is not one.
SEARCH_LIMIT = 8
SEARCH_MIN_CHARS = 2

#: The ceiling on one bulk action. A bulk route with no ceiling is a way to
#: hold a worker open for a minute.
BULK_MAX = 200
