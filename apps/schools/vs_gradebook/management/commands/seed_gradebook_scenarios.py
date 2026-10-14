"""Put results behind the gradebook screens, so every state has a row to look at.

A screen cannot be checked against an endpoint that returns nothing, and the
states a term passes through cannot all be reached by hand in an afternoon.
This command builds them in the two live schools the sibling seeders leave
behind, for the school's current term by its own clock:

    sunrise-academy   One branch. The recede case: one continuous assessment
                      of 40 and an examination of 60, one A to F scale, no
                      ranking, no ratings, no moderation, and so no position
                      anywhere on a card.
    lagoon-view       Two branches. Continuous assessments of 20 and 20 and an
                      examination of 60, graded A to F at Junior Secondary and
                      A1 to F9 at Senior Secondary. Senior Secondary is
                      moderated, the Sciences department has a head, and the
                      report card options are on (class figures, position,
                      conduct ratings and their key).

**Which class plays which part** is decided by size, as the attendance seeder
does, because the student seeder fills its classes unevenly and a state needs
pupils to show: within a branch and a programme the largest class takes the
first role that wants one, the next largest the second. On the rolls
``seed_student_scenarios`` builds this is:

**Lagoon View** (an A class is in the main branch, a B class in the second):

* JSS1 A is PUBLISHED. Two results were held before publication; one stays held
  (with its reason) and the other was lifted afterwards, which released it and
  wrote to the guardian. It carries an excused component and an absent cell.
* SSS1 A is AWAITING the principal, with the principal's comments written and
  one result held. Further Mathematics and Economics are taken by some of the
  class, so their sheets list only those pupils. Mathematics and Further
  Mathematics were moderated and APPROVED; the other sheets are in without
  moderation because their departments have no head.
* JSS2 A was submitted and then returned by the principal to its class teacher,
  so it is back in COMPILING with the note standing.
* JSS1 B is COMPILING: every sheet is in (Yoruba is taken by a third of the
  class) and the class teacher has commented on half of the roll.
* JSS2 B, which has only two pupils, was submitted and the principal then
  returned one subject's sheet to its teacher, so it is back to ENTERING with
  that sheet RETURNED.
* SSS1 B is ENTERING: Mathematics is SUBMITTED and waiting for the head, Further
  Mathematics was RETURNED by the head with a note, English is IN_PROGRESS with
  blanks and has been reminded, Civic Education is NOT_STARTED, and Economics is
  in with an absent examination.

**Sunrise Academy**: the largest Junior Secondary class is PUBLISHED and the
next is ENTERING (two sheets in, one in progress, one not started).

The remaining classes are left with nothing entered, which is also a state.

**Build-two states** (Lagoon View only):

* JSS1 A's published card has three corrections raised by the main branch
  administrator, each for a pupil whose result was released: one APPROVED by the
  principal (so the card is on its second version and the guardian is sent the
  "updated" notice), one PENDING and waiting for her, and one REJECTED with a
  note. The administrator who raises a correction is never the one who decides
  it.
* SSS1 B's English Language sheet, still in progress with blanks, had part of
  its marks entered from an uploaded spreadsheet: the file was checked, then
  confirmed, with new, unchanged and replacing cells, and two pupils' blanks
  were filled. The sheet is still in progress. An upload names pupils by
  admission number and the student seeder leaves the number blank (the school
  does not issue them automatically), so the pupils on that sheet are numbered
  first, the way the student record's own update does it.
* **Earlier terms of the school year.** When the day falls after the school
  year's first term, JSS1 A is brought to PUBLISHED for each earlier term of the
  year first, with marks that sit a few points above or below the current
  term's (alternating down the roll), so the analytics' term-on-term movers
  have pupils who rose and pupils who fell. Because they are published before
  the current term, a current term that is the last of its year publishes with
  the cumulative block, so a cumulative card renders. The earlier terms go
  through the same services as the current one, term by term.
* When the day falls in the school year's first term there is nothing earlier to
  publish in that year. The previous year is inspected and left alone: the
  student seeder places pupils in the active year only, so a class of the
  previous year has an empty roll, and a class with no pupils cannot be
  published. The run says so on a line beginning ``- `` and does not count it as
  created.

**Everything goes through the real services.** Settings come from
``configuration.write_settings``, structures and scales from ``setup``, who takes
what from the academic module's ``electives.apply_changes``, teachers from the
staff module's own invitation path and ``teaching.assign`` and
``set_class_teacher``, scores from ``sheets.save_scores`` and ``submit``,
moderation from ``moderation.approve`` and ``return_sheet``, reminders from
``reminders.remind``, comments from ``compiling.save_compile`` and
``submit_class``, returns and the principal's comments from ``approvals``,
publication from ``publishing.publish_class`` and holds from ``holds.place``
and ``lift``. A state that cannot be reached honestly fails loudly here instead
of being written as a row and believed.

**Created outside the gradebook.** A lead teacher for every sheet, a class
teacher for every class that lacks one able to submit it, an elective's takers
and a department head did not exist in the prerequisite data. Teachers and the
bursar are invited through the staff seeder's own ``_invite`` (the same path the
attendance seeder uses), leads and class teachers go through the staff module's
``teaching`` service, and takers through the electives service. The Sciences
department has no service for naming a head, so its ``head`` is set the way the
academic department endpoint sets it: the member of staff is resolved by
``heads.resolve_head`` (on the roll, this school's) and an audit event is
written. An existing lead who could not enter scores, such as a bursar the staff
seeder named, is stepped back to assistant first, as the module requires.

**Corrections** are raised from the school's branch administrator account
(``branch.admin@<slug>.example.com``, built with the school) and decided by the
school's administrator, because the person who raises a correction may not
decide it. Each is judged by its reason, so a rerun finds it by state; a
correction left pending by a run that stopped is decided on the next.

**Marks** are generated from a stable hash of the pupil's admission number, the
subject and the component, so a rerun and a fresh database agree.

**Idempotent.** Every step looks at the state it would produce before acting: a
sheet already submitted, a class already awaiting, a hold already placed and a
reminder already sent are left alone and print nothing. Run twice, the second
run prints ``Created this run: 0``. Nothing is wrapped in one transaction, so a
run that fails part way is simply run again.

**Email.** The run forces Django's in-memory mail backend, so no seeded address
is ever mailed. The in-app notifications are written as usual.

**Guardians with no email.** The student seeder gives every household an
address, so the published class has no ``NO_EMAIL`` result unless
``seed_attendance_scenarios`` has run first: it enrols one child whose household
gave none into the largest main-branch class, which is JSS1 A. Nothing here
fabricates one.

    python manage.py seed_gradebook_scenarios
    python manage.py seed_gradebook_scenarios --only lagoon-view

Run ``seed_all_permissions``, ``seed_notification_event_types``,
``seed_notification_templates``, ``seed_onboarding_scenarios``,
``seed_academic_scenarios`` and ``seed_student_scenarios`` first. Staff and
attendance seeders are optional. Never run against production.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    Department,
    Program,
    SchoolClass,
    SessionStatus,
    Subject,
)
from schools.vs_academics.services import electives as elective_service
from schools.vs_academics.services import heads
from schools.vs_gradebook.constants import (
    PERM_AMENDMENT_APPROVE,
    PERM_AMENDMENT_CREATE,
    PERM_CLASS_SUBMIT,
    PERM_SHEET_ENTER,
    PERM_SHEET_IMPORT,
    AmendmentStatus,
    ClassStage,
    SetupStatus,
    SheetStatus,
    StoredStage,
)
from schools.vs_gradebook.models import (
    Amendment,
    AssessmentStructure,
    ClassTerm,
    GradingScale,
    PrincipalComment,
    ResultHold,
    ScoreSheet,
    SheetReminder,
    StudentTermResult,
)
from schools.vs_gradebook.services import (
    amendments,
    approvals,
    book,
    classbook,
    compiling,
    configuration,
    cumulative,
    figures,
    holds,
    importing,
    moderation,
    publishing,
    reminders,
    resolve,
    setup,
    sheets,
    spreadsheet,
)
from schools.vs_gradebook.services.rules import read_gradebook_rules
from schools.vs_staff.constants import EmploymentStatus, TeachingPart
from schools.vs_staff.management.commands.seed_staff_scenarios import (
    SEED_PASSWORD,
)
from schools.vs_staff.management.commands.seed_staff_scenarios import (
    Command as StaffSeeder,
)
from schools.vs_staff.models import StaffProfile, TeachingAssignment
from schools.vs_staff.services import teaching
from schools.vs_students.models import Student
from schools.vs_students.services import enrolment as enrolment_service
from schools.vs_students.services.policy import assert_number_allowed
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event
from vs_config.clock import tenant_today
from vs_rbac.evaluator import has_permission

CAST = ("sunrise-academy", "lagoon-view")

# ── What a sheet or a class is brought to ──────────────────────────────────

#: Sheet goals. ``IN`` is "counts toward the class": submitted, and approved by
#: the department head when the sheet is moderated. ``SUBMITTED`` stops at the
#: submit, so a moderated sheet stays waiting for its head.
NOT_STARTED, IN_PROGRESS, SUBMITTED, IN, APPROVED, RETURNED = (
    "NOT_STARTED", "IN_PROGRESS", "SUBMITTED", "IN", "APPROVED", "RETURNED",
)

#: Class goals, in the order a term moves.
ENTERING, COMPILING, CLASS_RETURNED, SHEET_RETURNED, AWAITING, PUBLISHED = (
    "ENTERING", "COMPILING", "CLASS_RETURNED", "SHEET_RETURNED", "AWAITING", "PUBLISHED",
)

ABS, EXC = "ABS", "EXC"

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Students left wholly blank on an in-progress sheet.
BLANK_STUDENTS = 3

#: The fewest pupils a class needs to carry a role, unless the role says otherwise.
MIN_ROLL = 4

#: The pupils at the end of an in-progress sheet that an upload completes: the
#: one left half entered and the first of the wholly blank, so the sheet still
#: has a pupil with nothing entered after it.
UPLOAD_FILLS = 2

#: The account the corrections are raised from, at a school that has them. The
#: school's own administrator, who holds the approving key, decides them.
RAISER = "branch.admin"


@dataclass(frozen=True)
class Hold:
    """A result held by hand: the pupil's place on the roll, why, and the note it is lifted with."""

    index: int
    reason: str
    lift_note: str | None = None


@dataclass(frozen=True)
class Correction:
    """A correction to a published card: whose, which mark, how far it moves and how it ends.

    ``index`` is the pupil's place on the roll and ``component`` the short name
    of the column corrected. The mark moves by ``change`` upwards, or downwards
    where that would pass the column's maximum. ``decision`` is PENDING (left
    waiting), APPROVED or REJECTED, and ``note`` the principal's note with it.
    """

    index: int
    subject: str
    component: str
    change: int
    reason: str
    decision: str
    note: str = ""


@dataclass(frozen=True)
class ClassPlan:
    """One role in the cast and the state its class is brought to.

    ``branch`` is the branch's place (0 for the main branch) and ``program`` the
    programme code of the class that plays it; ``min_roll`` is the fewest pupils
    it can be shown with. ``sheets`` overrides the goal of named subjects (every other due sheet is
    brought ``IN``). ``electives`` gives each optional subject a share of the
    roll as ``(modulus, residues)`` of the pupil's place. ``cells`` marks a
    pupil's component ABS or EXC as ``(subject, place, component short name)``.
    ``corrections`` are raised against the card once published and ``uploads``
    name in-progress subjects whose sheet is part filled from a spreadsheet.
    ``history`` brings the class to published for each earlier term of the year
    before this one, and ``drift`` marks a plan whose marks sit a little above or
    below the generated ones, so a term played against the current one shows
    pupils who rose and pupils who fell.
    """

    label: str
    branch: int
    program: str
    teacher: str
    goal: str
    min_roll: int = MIN_ROLL
    sheets: dict = field(default_factory=dict)
    electives: dict = field(default_factory=dict)
    cells: dict = field(default_factory=dict)
    holds: tuple = ()
    return_note: str = ""
    returned_sheet: str = ""
    sheet_notes: dict = field(default_factory=dict)
    remind: tuple = ()
    corrections: tuple = ()
    uploads: tuple = ()
    history: bool = False
    drift: bool = False


@dataclass(frozen=True)
class Person:
    """A member of staff the seeder needs: where they sit and what they teach."""

    key: str
    first: str
    last: str
    gender: str
    title: str
    where: str
    number: int
    subjects: tuple = ()


STRUCTURES = {
    "split": (
        ("Continuous Assessment", "CA", 40, False),
        ("Examination", "EXAM", 60, True),
    ),
    "three": (
        ("1st CA", "CA1", 20, False),
        ("2nd CA", "CA2", 20, False),
        ("Examination", "EXAM", 60, True),
    ),
}

SCALES = {
    "letters": (
        (70, "A", "Excellent", False),
        (60, "B", "Very good", False),
        (50, "C", "Good", False),
        (40, "D", "Fair", False),
        (0, "F", "Fail", True),
    ),
    "waec": (
        (75, "A1", "Excellent", False),
        (70, "B2", "Very good", False),
        (65, "B3", "Good", False),
        (60, "C4", "Credit", False),
        (55, "C5", "Credit", False),
        (50, "C6", "Credit", False),
        (45, "D7", "Pass", False),
        (40, "E8", "Pass", False),
        (0, "F9", "Fail", True),
    ),
}

#: ``{slug: {programme code: (structure, scale)}}``.
SETUP = {
    "sunrise-academy": {
        "JSS": ("split", "letters"),
        "SSS": ("split", "letters"),
    },
    "lagoon-view": {
        "JSS": ("three", "letters"),
        "SSS": ("three", "waec"),
    },
}

#: ``{slug: settings the school writes}``, keyed by the settings screen's fields.
SETTINGS = {
    "sunrise-academy": {
        "rank_in_class": False,
        "conduct_ratings": False,
        "rating_key": False,
        "moderated_programs": [],
    },
    "lagoon-view": {
        "show_class_stats": True,
        "rank_in_class": True,
        "conduct_ratings": True,
        "rating_key": True,
        "moderated_programs": ["SSS"],
    },
}

#: The department whose head moderates, and who heads it, by school.
HEADED_DEPARTMENT = "Sciences"
HEADS = {"lagoon-view": "bakare"}
BURSAR = "ibekwe"

PEOPLE = {
    "sunrise-academy": (
        Person("okeke", "Chinedu", "Okeke", "MALE", "Mathematics Teacher", "main", 701,
               ("Mathematics", "Basic Science")),
        Person("garba", "Amina", "Garba", "FEMALE", "English Teacher", "main", 702,
               ("English Language",)),
        Person("fashola", "Tolu", "Fashola", "FEMALE", "Teacher", "main", 703,
               ("Civic Education",)),
        Person("ibekwe", "Ngozi", "Ibekwe", "FEMALE", "Bursar", "any", 708),
    ),
    "lagoon-view": (
        Person("okeke", "Chinedu", "Okeke", "MALE", "Mathematics Teacher", "main", 701,
               ("Mathematics", "Further Mathematics")),
        Person("garba", "Amina", "Garba", "FEMALE", "English Teacher", "main", 702,
               ("English Language",)),
        Person("fashola", "Tolu", "Fashola", "FEMALE", "Teacher", "main", 703,
               ("Basic Science", "Civic Education", "Economics")),
        Person("adeniyi", "Kayode", "Adeniyi", "MALE", "Mathematics Teacher", "second", 704,
               ("Mathematics", "Further Mathematics")),
        Person("sanni", "Hauwa", "Sanni", "FEMALE", "English Teacher", "second", 705,
               ("English Language",)),
        Person("alabi", "Bukky", "Alabi", "FEMALE", "Teacher", "second", 706,
               ("Basic Science", "Civic Education", "Economics", "Yoruba")),
        Person("bakare", "Oluwaseun", "Bakare", "MALE", "Head of Sciences", "any", 707),
        Person("ibekwe", "Ngozi", "Ibekwe", "FEMALE", "Bursar", "any", 708),
    ),
}

PLANS = {
    "sunrise-academy": (
        ClassPlan(
            "published", 0, "JSS", "garba", PUBLISHED,
            cells={("Mathematics", 1, "CA"): EXC, ("Civic Education", 2, "EXAM"): ABS},
        ),
        ClassPlan(
            "entering", 0, "JSS", "okeke", ENTERING,
            sheets={
                "Mathematics": SUBMITTED, "English Language": SUBMITTED,
                "Basic Science": IN_PROGRESS, "Civic Education": NOT_STARTED,
            },
        ),
    ),
    "lagoon-view": (
        ClassPlan(
            "published", 0, "JSS", "garba", PUBLISHED,
            cells={("Mathematics", 1, "CA1"): EXC, ("Civic Education", 2, "CA2"): ABS},
            holds=(
                Hold(4, "Two terms of fees unpaid; the family asked to see the bursar."),
                Hold(7, "Cheque not yet cleared.",
                     lift_note="Cheque cleared; paid in full at the bursary."),
            ),
            history=True,
            corrections=(
                Correction(
                    2, "Mathematics", "EXAM", 6,
                    "The examination script was marked again after the family asked, and "
                    "question 4 had been left out of the first total.",
                    AmendmentStatus.APPROVED,
                    "Checked against the script; the corrected total is right.",
                ),
                Correction(
                    3, "English Language", "CA2", 3,
                    "The second continuous assessment was entered from the wrong column "
                    "of the teacher's notebook.",
                    AmendmentStatus.PENDING,
                ),
                Correction(
                    5, "Basic Science", "EXAM", 9,
                    "The family believes the examination mark is too low.",
                    AmendmentStatus.REJECTED,
                    "The script was looked at again and the first mark stands.",
                ),
            ),
        ),
        ClassPlan(
            "awaiting", 0, "SSS", "okeke", AWAITING,
            electives={"Further Mathematics": (2, (0,)), "Economics": (3, (1, 2))},
            holds=(Hold(3, "Examination levy outstanding."),),
        ),
        ClassPlan(
            "class returned", 0, "JSS", "fashola", CLASS_RETURNED,
            return_note="Several comments repeat word for word. Please write each one "
                        "for the child it describes.",
        ),
        ClassPlan(
            "compiling", 1, "JSS", "sanni", COMPILING,
            electives={"Yoruba": (3, (0,))},
        ),
        ClassPlan(
            "entering", 1, "SSS", "adeniyi", ENTERING,
            electives={"Further Mathematics": (2, (1,)), "Economics": (3, (0, 1))},
            sheets={
                "Mathematics": SUBMITTED, "Further Mathematics": RETURNED,
                "English Language": IN_PROGRESS, "Civic Education": NOT_STARTED,
            },
            cells={("Economics", 0, "EXAM"): ABS},
            sheet_notes={
                "Further Mathematics": "Two columns of continuous assessment marks "
                                       "look swapped. Please recheck and resubmit.",
            },
            remind=("English Language",),
            uploads=("English Language",),
        ),
        ClassPlan(
            "sheet returned", 1, "JSS", "alabi", SHEET_RETURNED, min_roll=2,
            returned_sheet="Basic Science",
            return_note="The examination column looks transposed. Please recheck "
                        "against the scripts.",
        ),
    ),
}

CLASS_TEACHER_COMMENTS = (
    "A focused and polite pupil who is making steady progress.",
    "Takes part well in class and should keep reading widely.",
    "Capable of more with greater consistency in homework.",
    "A cheerful team player whose results are improving.",
    "Should ask for help sooner when a topic is difficult.",
)

#: Added to the principal's template for a band, by lowest average of the band.
PRINCIPAL_ADDENDA = {70: " I am proud of this class.", 55: " Keep working steadily."}


# ── Marks ──────────────────────────────────────────────────────────────────

def stable_hash(text: str) -> int:
    """A 32-bit polynomial hash of *text*, the same on every machine and run."""
    value = 0
    for character in text:
        value = (value * 31 + ord(character)) & 0xFFFFFFFF
    return value


def pupil_key(student) -> str:
    """What identifies a pupil the same way on every database: admission number, else name and birth date."""
    return student.student_number or (
        f"{student.first_name}{student.last_name}{student.date_of_birth}"
    )


def generated_mark(student, subject, component) -> int:
    """A believable whole mark for the pupil in the component, stable across runs.

    The spread covers every band of both scales, so the grade distribution,
    the pass rate and the positions all have something to show.
    """
    spread = stable_hash(f"{pupil_key(student)}{subject.code}{component.short_name}")
    fraction = 0.40 + ((spread % 1000) / 1000) * 0.50
    ability = ((stable_hash(pupil_key(student)) % 31) - 20) / 100
    value = component.max_score * min(1.0, max(0.0, fraction + ability))
    return max(0, min(component.max_score, math.floor(value + 0.5)))


class Command(BaseCommand):
    help = "Seed score sheets, class results, approvals, publication and fee holds for two live schools."

    def add_arguments(self, parser):
        parser.add_argument("--only", help="One school slug from the cast.")

    def handle(self, *args, **options):
        only = options.get("only")
        if only and only not in CAST:
            raise CommandError(
                f"{only!r} is not in the cast. Pick one of: {', '.join(CAST)}",
            )
        self.created = 0
        logins = []
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        ):
            for slug in ([only] if only else list(CAST)):
                logins.extend(self._seed(slug))
        self.stdout.write(self.style.SUCCESS(f"\nCreated this run: {self.created}"))
        self.stdout.write(
            "\nLogins. Each school is reached at <slug>.localhost:5174. The admin "
            "account takes the dev fixture password (DEFAULT_PASSWORD in "
            "schools/vs_schools/dev/fixtures.py); the teachers, the head of "
            f"department and the bursar take {SEED_PASSWORD}.",
        )
        for line in logins:
            self.stdout.write(f"  {line}")

    # ── plumbing ──────────────────────────────────────────────────────────

    def _made(self, text):
        self.created += 1
        self.stdout.write(f"  + {text}")

    def _school(self, slug):
        from schools.vs_schools.models import School

        school = School.objects.filter(slug=slug).first()
        if school is None:
            raise CommandError(
                f"No school {slug!r}. Run seed_onboarding_scenarios, "
                f"seed_academic_scenarios and seed_student_scenarios first.",
            )
        return school

    def _admin(self, tenant, slug):
        from vs_user.models import User

        email = f"admin@{slug}.example.com"
        user = User.objects.filter(tenant=tenant, email=email).first()
        if user is None:
            raise CommandError(
                f"{slug} has no account {email}. Run seed_onboarding_scenarios first.",
            )
        return user

    # ── the school ────────────────────────────────────────────────────────

    def _seed(self, slug):
        school = self._school(slug)
        tenant = school.tenant
        branches = list(tenant.branches.order_by("-is_main", "code"))
        year = AcademicSession.objects.filter(
            tenant=tenant, status=SessionStatus.ACTIVE,
        ).first()
        if year is None:
            raise CommandError(f"{slug} has no active year. Run seed_academic_scenarios.")
        today = tenant_today(tenant)
        term = resolve.current_term(tenant, today)
        if term is None or today < term.start_date:
            raise CommandError(f"{slug} has no term running on {today}.")

        s = SimpleNamespace(
            slug=slug, school=school, tenant=tenant, admin=self._admin(tenant, slug),
            branches=branches, year=year, term=term, today=today, people={},
        )
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{school.name} ({slug}), {len(branches)} "
            f"branch{'es' if len(branches) > 1 else ''}, {term.name} of {year.name}, "
            f"today is {today}",
        ))
        self._settings(s)
        self._setup(s)
        self._people(s)
        self._department_head(s)
        s.cast = self._pick_classes(s)
        for plan in PLANS[slug]:
            if plan.history:
                self._earlier_terms(s, plan)
            self._class(s, plan)
            if plan.history:
                self._cumulative_card(s, plan)
        self._summary(s)

        lines = [f"{slug}: admin@{slug}.example.com (whole school, publishes)"]
        if any(plan.corrections for plan in PLANS[slug]):
            lines.append(
                f"{slug}: {RAISER}@{slug}.example.com (branch administrator, raises "
                f"corrections)",
            )
        for person in PEOPLE[slug]:
            lines.append(f"{slug}: {self._email(s, person)} ({person.title})")
        return lines

    # ── settings, structures and scales ───────────────────────────────────

    def _settings(self, s):
        before = read_gradebook_rules(s.tenant)
        after = configuration.write_settings(s.tenant, s.admin, SETTINGS[s.slug])
        if before != after:
            self._made("gradebook settings written")

    def _setup(self, s):
        for code, (structure_key, scale_key) in SETUP[s.slug].items():
            program = Program.all_objects.filter(tenant=s.tenant, code=code).first()
            if program is None:
                raise CommandError(f"{s.slug} has no programme {code}.")
            self._in_use(
                s, setup.STRUCTURE, AssessmentStructure, program, structure_key,
                f"{program.name} assessment",
                [
                    {"name": name, "short_name": short, "max_score": top, "is_exam": exam}
                    for name, short, top, exam in STRUCTURES[structure_key]
                ],
            )
            self._in_use(
                s, setup.SCALE, GradingScale, program, scale_key,
                f"{program.name} grading",
                [
                    {"min_score": low, "grade": grade, "remark": remark, "is_fail": fail}
                    for low, grade, remark, fail in SCALES[scale_key]
                ],
            )

    def _in_use(self, s, kind, model, program, _key, name, rows):
        """Leave a school-wide record in use for the programme, creating and activating one if none is."""
        if model.objects.filter(
            tenant=s.tenant, program=program, branch__isnull=True,
            status=SetupStatus.IN_USE,
        ).exists():
            return
        draft = model.objects.filter(
            tenant=s.tenant, program=program, branch__isnull=True, name=name,
            status=SetupStatus.DRAFT,
        ).first()
        if draft is None:
            draft = setup.create_record(
                kind, s.tenant, s.admin,
                {"name": name, "program": program.pk, "branch": None, kind.child_attr: rows},
            )
        setup.activate_record(kind, s.tenant, s.admin, draft)
        self._made(f"{kind.noun} in use: {name}")

    # ── people ────────────────────────────────────────────────────────────

    @staticmethod
    def _email(s, person) -> str:
        return f"{person.first}.{person.last}@{s.slug}.example.com".lower()

    def _home(self, s, person):
        """The branch a person is posted to, or None for somebody with no home branch."""
        if len(s.branches) == 1:
            return s.branches[0]
        return {"main": s.branches[0], "second": s.branches[1], "any": None}[person.where]

    def _people(self, s):
        """Invite each person the plans need, once, and index them by key."""
        staff_seeder = StaffSeeder()
        staff_seeder.stdout = self.stdout
        for person in PEOPLE[s.slug]:
            email = self._email(s, person)
            profile = StaffProfile.objects.filter(
                tenant=s.tenant, user__email=email,
            ).select_related("user").first()
            if profile is None:
                profile = staff_seeder._invite(
                    tenant=s.tenant, actor=s.admin, first=person.first, last=person.last,
                    gender=person.gender, job_title=person.title,
                    branch=self._home(s, person), email=email, number=person.number,
                    slug=s.slug,
                )
                if profile is None:
                    raise CommandError(f"Could not invite {email}.")
                staff_seeder._drive_to(profile, EmploymentStatus.ACTIVE, s.admin)
                self._made(f"{person.title} {person.first} {person.last} ({email})")
            s.people[person.key] = profile

    def _department_head(self, s):
        """Name the head of the moderated department, as the department endpoint does."""
        key = HEADS.get(s.slug)
        if key is None:
            return
        department = Department.all_objects.filter(
            tenant=s.tenant, name=HEADED_DEPARTMENT,
        ).first()
        if department is None:
            raise CommandError(f"{s.slug} has no {HEADED_DEPARTMENT} department.")
        if department.head_id is not None:
            return
        department.head = heads.resolve_head(s.tenant, s.people[key].pk)
        department.save()
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS, action_type=AuditActionType.UPDATE,
            entity_type="Department", entity_id=str(department.pk),
            entity_label=department.name, tenant=s.tenant, actor_user=s.admin,
            summary=f"{department.name} updated.",
        )
        self._made(f"head of {department.name}: {s.people[key].user.full_name}")

    def _person_for(self, s, home_index, subject_name):
        place = "main" if home_index == 0 else "second"
        for person in PEOPLE[s.slug]:
            if subject_name in person.subjects and person.where == place:
                return person
        raise CommandError(f"{s.slug} has nobody for {subject_name} in the {place} branch.")

    # ── a class ───────────────────────────────────────────────────────────

    def _pick_classes(self, s):
        """``{role: (class, branch)}``: the largest free class of the role's branch and programme.

        Roles are filled in plan order, so the first role that wants a Junior
        Secondary class of the main branch gets the fullest one. A role no class
        can fill (too few pupils, or no class of that programme in the branch)
        stops the run, because a state shown on nobody proves nothing.
        """
        classes = list(
            SchoolClass.objects.filter(tenant=s.tenant, session=s.year, is_active=True)
            .select_related("level", "level__program", "branch", "session",
                            "class_teacher", "class_teacher__user")
        )
        placed = [(school_class, resolve.class_branch(s.tenant, school_class))
                  for school_class in classes]
        rolls = book.rolls_for(s.tenant, s.term, placed)
        taken, cast = set(), {}
        for plan in PLANS[s.slug]:
            branch = s.branches[min(plan.branch, len(s.branches) - 1)]
            candidates = sorted(
                (
                    (-len(rolls.get(school_class.pk, [])), school_class.name, school_class)
                    for school_class, home in placed
                    if home.pk == branch.pk and school_class.pk not in taken
                    and school_class.level.program.code == plan.program
                    and len(rolls.get(school_class.pk, [])) >= plan.min_roll
                ),
                key=lambda row: row[:2],
            )
            if not candidates:
                raise CommandError(
                    f"{s.slug} has no {plan.program} class in {branch.name} with at "
                    f"least {plan.min_roll} pupils for the {plan.label} role. Run "
                    f"seed_student_scenarios.",
                )
            school_class = candidates[0][2]
            taken.add(school_class.pk)
            cast[plan.label] = (school_class, branch)
        return cast

    def _class(self, s, plan):
        school_class, home = s.cast[plan.label]
        c = SimpleNamespace(
            plan=plan, cls=school_class, home=home,
            index=[b.pk for b in s.branches].index(home.pk),
        )
        self.stdout.write(
            f"\n  {school_class.name} ({home.name}), {s.term.name}: {plan.goal}",
        )

        self._electives(s, c)
        self._class_teacher(s, c)
        self._leads(s, c)
        due = resolve.due_subjects(s.tenant, school_class, s.term)
        self._check_plan(s, c, {subject.name for subject in due})
        for subject in due:
            self._sheet(s, c, subject)
        for name in plan.remind:
            self._remind(s, c, name)
        for name in plan.uploads:
            self._upload(s, c, name)
        if plan.goal != ENTERING:
            self._advance(s, c)
        self._expect_stage(s, c)
        if plan.corrections:
            self._corrections(s, c)

    def _check_plan(self, s, c, due_names):
        """Refuse a plan that names a subject the class owes no sheet for."""
        named = (
            set(c.plan.sheets) | {subject for subject, _, _ in c.plan.cells}
            | set(c.plan.remind) | set(c.plan.uploads)
            | {fix.subject for fix in c.plan.corrections}
        )
        absent = sorted(named - due_names)
        if absent:
            raise CommandError(
                f"{c.cls.name} owes no sheet for {', '.join(absent)}, so the "
                f"{c.plan.label} role cannot be shown on it.",
            )

    def _expect_stage(self, s, c):
        """Fail unless the class ended in the stage its goal names."""
        wanted = {
            ENTERING: ClassStage.ENTERING, SHEET_RETURNED: ClassStage.ENTERING,
            COMPILING: ClassStage.COMPILING, CLASS_RETURNED: ClassStage.COMPILING,
            AWAITING: ClassStage.AWAITING, PUBLISHED: ClassStage.PUBLISHED,
        }[c.plan.goal]
        found = book.load(s.tenant, s.term, [(c.cls, c.home)])[0].stage
        if found != wanted:
            raise CommandError(
                f"{c.cls.name} should be {wanted} for the {c.plan.label} role and is "
                f"{found}.",
            )

    def _roll(self, s, c):
        """The pupils on the class roll for the term, in name order."""
        entries = book.rolls_for(s.tenant, s.term, [(c.cls, c.home)])[c.cls.pk]
        return [entry.student for entry in entries]

    # ── who takes what, who teaches ───────────────────────────────────────

    def _electives(self, s, c):
        if not c.plan.electives:
            return
        roll = self._roll(s, c)
        recorded = elective_service.takers(s.tenant, c.cls, s.term)
        changes = []
        for subject_name, (modulus, residues) in c.plan.electives.items():
            subject = self._subject(s, subject_name)
            for place, student in enumerate(roll):
                if place % modulus in residues and student.pk not in recorded.get(subject.pk, ()):
                    changes.append({"student": student.pk, "subject": subject.pk, "takes": True})
        if not changes:
            return
        elective_service.apply_changes(
            s.tenant, c.cls, s.term, changes, actor=s.admin, today=s.today,
        )
        self._made(f"{len(changes)} elective choices in {c.cls.name}")

    def _subject(self, s, name):
        subject = Subject.all_objects.filter(tenant=s.tenant, name=name).first()
        if subject is None:
            raise CommandError(f"{s.slug} has no subject {name}.")
        return subject

    def _class_teacher(self, s, c):
        current = c.cls.class_teacher
        if (
            current is not None and current.is_on_roll
            and has_permission(current.user, PERM_CLASS_SUBMIT, tenant=s.tenant)
        ):
            return
        staff = s.people[c.plan.teacher]
        teaching.set_class_teacher(c.cls, staff, actor=s.admin, tenant=s.tenant)
        c.cls.refresh_from_db()
        self._made(f"class teacher of {c.cls.name}: {staff.user.full_name}")

    def _leads(self, s, c):
        """Make sure every due subject has a main teacher who can enter its scores."""
        for subject in resolve.due_subjects(s.tenant, c.cls, s.term):
            held = TeachingAssignment.objects.filter(
                tenant=s.tenant, session=s.year, school_class=c.cls, subject=subject,
                part=TeachingPart.LEAD,
            ).select_related("staff", "staff__user").first()
            if held is not None:
                if held.staff.is_on_roll and has_permission(
                    held.staff.user, PERM_SHEET_ENTER, tenant=s.tenant,
                ):
                    continue
                teaching.set_part(held, TeachingPart.ASSISTANT, actor=s.admin)
            person = self._person_for(s, c.index, subject.name)
            teaching.assign(
                tenant=s.tenant, staff=s.people[person.key], school_class=c.cls,
                subject=subject, session=s.year, part=TeachingPart.LEAD, actor=s.admin,
            )
            self._made(f"{person.first} {person.last} leads {subject.name} in {c.cls.name}")

    # ── sheets ────────────────────────────────────────────────────────────

    def _status(self, s, c, subject):
        found = ScoreSheet.objects.filter(
            tenant=s.tenant, class_term__school_class=c.cls, class_term__term=s.term,
            subject=subject,
        ).first()
        return found.status if found is not None else SheetStatus.NOT_STARTED, found

    def _lead_user(self, s, c, subject):
        return resolve.main_teacher(c.cls, subject, c.cls.session_id).user

    def _load(self, s, c, subject, user, *, rule=sheets.READ):
        return sheets.load(s.tenant, user, c.cls.pk, subject.pk, s.term, rule=rule)

    @staticmethod
    def _mark(c, place, student, subject, component) -> int:
        """The generated mark, moved a few points for a plan that drifts.

        A drifting plan is an earlier term. Pupils at even places did worse then
        than the generated mark says (so they rise to it), those at odd places
        did better (so they fall to it). The size of the move is a stable
        function of the pupil and the subject, between 4 and 11 percent of the
        column.
        """
        mark = generated_mark(student, subject, component)
        if not c.plan.drift:
            return mark
        step = 4 + stable_hash(f"{pupil_key(student)}{subject.code}drift") % 8
        move = math.floor(component.max_score * step / 100 + 0.5)
        mark = mark - move if place % 2 == 0 else mark + move
        return max(0, min(component.max_score, mark))

    def _cells(self, c, ctx, *, full, partial=False):
        """Cells for the first *full* pupils, and the first column only of the next when *partial*."""
        markers = {
            (place, short): marker
            for (subject_name, place, short), marker in c.plan.cells.items()
            if subject_name == ctx.subject.name
        }
        cells = []
        for place, student in enumerate(ctx.students):
            if place >= full + (1 if partial else 0):
                break
            for number, component in enumerate(ctx.components):
                if place >= full and number > 0:
                    break
                marker = markers.get((place, component.short_name))
                cells.append({
                    "student": student.pk, "component": component.pk,
                    "mark": None if marker else self._mark(c, place, student, ctx.subject, component),
                    "marker": marker,
                })
        return cells

    def _sheet(self, s, c, subject):
        goal = c.plan.sheets.get(subject.name, IN)
        status, _ = self._status(s, c, subject)
        label = f"{subject.name}, {c.cls.name}"
        lead = self._lead_user(s, c, subject)

        if goal == NOT_STARTED:
            return
        if goal == IN_PROGRESS:
            if status == SheetStatus.NOT_STARTED:
                ctx = self._load(s, c, subject, lead, rule=sheets.ENTER)
                full = max(0, len(ctx.students) - BLANK_STUDENTS)
                sheets.save_scores(ctx, self._cells(c, ctx, full=full, partial=True))
                self._made(f"{label} in progress with blanks")
            return

        if status in (SheetStatus.NOT_STARTED, SheetStatus.IN_PROGRESS):
            ctx = self._load(s, c, subject, lead, rule=sheets.ENTER)
            sheets.save_scores(ctx, self._cells(c, ctx, full=len(ctx.students)))
            sheets.submit(self._load(s, c, subject, lead, rule=sheets.ENTER))
            self._made(f"{label} submitted")
            status = SheetStatus.SUBMITTED
        if goal == SUBMITTED:
            return

        _, sheet = self._status(s, c, subject)
        if status == SheetStatus.SUBMITTED and sheet.needs_moderation:
            head = s.people[HEADS[s.slug]].user
            ctx = self._load(s, c, subject, head)
            if goal == RETURNED:
                moderation.return_sheet(ctx, c.plan.sheet_notes[subject.name])
                self._made(f"{label} returned by {head.full_name}")
            else:
                moderation.approve(ctx)
                self._made(f"{label} approved by {head.full_name}")
            return
        wanted = {APPROVED: SheetStatus.APPROVED, RETURNED: SheetStatus.RETURNED}.get(goal)
        if wanted is not None and status != wanted:
            raise CommandError(
                f"{label} cannot be {goal.lower()}: it is {status.lower()} and "
                f"{'not moderated' if not sheet.needs_moderation else 'not waiting'}.",
            )

    def _remind(self, s, c, subject_name):
        subject = self._subject(s, subject_name)
        _, sheet = self._status(s, c, subject)
        if sheet is not None and SheetReminder.objects.filter(
            tenant=s.tenant, sheet=sheet,
        ).exists():
            return
        ctx = self._load(s, c, subject, s.admin, rule=sheets.ANY)
        reminders.remind(ctx, s.admin)
        self._made(f"reminder for {subject_name}, {c.cls.name}")

    # ── compiling the class ───────────────────────────────────────────────

    def _class_term(self, s, c):
        return ClassTerm.objects.filter(
            tenant=s.tenant, school_class=c.cls, term=s.term,
        ).first()

    def _comments(self, s, c, *, everyone):
        """The class teacher's comment, and ratings where the school records them, for the roll or half of it."""
        roll = self._roll(s, c)
        wanted = roll if everyone else roll[: max(1, len(roll) // 2)]
        class_term = self._class_term(s, c)
        done = set(
            StudentTermResult.objects.filter(tenant=s.tenant, class_term=class_term)
            .exclude(teacher_comment="").values_list("student_id", flat=True)
        ) if class_term is not None else set()
        rules = read_gradebook_rules(s.tenant)
        teacher = c.cls.class_teacher.user
        written = 0
        for student in wanted:
            if student.pk in done:
                continue
            ctx = classbook.open_class(
                s.tenant, teacher, c.cls.pk, s.term, rule=classbook.COMPILE,
            )
            ratings = (
                {
                    trait: 2 + stable_hash(f"{pupil_key(student)}{trait}") % 4
                    for trait in rules.rating_traits
                }
                if rules.conduct_ratings else None
            )
            comment = CLASS_TEACHER_COMMENTS[
                stable_hash(pupil_key(student)) % len(CLASS_TEACHER_COMMENTS)
            ]
            compiling.save_compile(ctx, student.pk, comment, ratings)
            written += 1
        if written:
            self._made(
                f"{written} comments in {c.cls.name} by {teacher.full_name}"
                f"{'' if everyone else ' (half of the roll)'}",
            )

    def _submit(self, s, c):
        class_term = self._class_term(s, c)
        if class_term is not None and class_term.stage is not None:
            return
        teacher = c.cls.class_teacher.user
        compiling.submit_class(classbook.open_class(
            s.tenant, teacher, c.cls.pk, s.term, rule=classbook.COMPILE,
        ))
        self._made(f"{c.cls.name} submitted to the principal")

    def _approver_ctx(self, s, c):
        return classbook.open_class(
            s.tenant, s.admin, c.cls.pk, s.term, rule=classbook.APPROVER,
        )

    def _principal_comments(self, s, c):
        class_term = self._class_term(s, c)
        if PrincipalComment.objects.filter(tenant=s.tenant, class_term=class_term).exists():
            return
        rows = [
            {"band_min": row["band_min"],
             "text": row["text"] + PRINCIPAL_ADDENDA.get(row["band_min"], "")}
            for row in setup.templates_payload(s.tenant)["templates"]
        ]
        approvals.save_principal_comments(self._approver_ctx(s, c), rows)
        self._made(f"principal's comments for {c.cls.name}")

    def _bursar(self, s):
        return s.people[BURSAR].user

    def _place_holds(self, s, c):
        roll = self._roll(s, c)
        for hold in c.plan.holds:
            if ResultHold.objects.filter(
                tenant=s.tenant, term=s.term, reason=hold.reason,
            ).exists():
                continue
            student = roll[hold.index]
            holds.place(s.tenant, self._bursar(s), s.term, student.pk, hold.reason)
            self._made(f"hold on {student.full_name} ({c.cls.name}): {hold.reason}")

    def _lift_holds(self, s, c):
        for hold in c.plan.holds:
            if hold.lift_note is None:
                continue
            found = ResultHold.objects.filter(
                tenant=s.tenant, term=s.term, reason=hold.reason, lifted_at__isnull=True,
            ).select_related("student").first()
            if found is None:
                continue
            holds.lift(s.tenant, self._bursar(s), found.pk, hold.lift_note)
            self._made(f"hold lifted on {found.student.full_name} ({c.cls.name})")

    def _advance(self, s, c):
        """Take the class from its sheets to its goal, doing only what its state calls for."""
        goal = c.plan.goal
        class_term = self._class_term(s, c)
        stage = class_term.stage if class_term is not None else None

        if goal == COMPILING:
            self._comments(s, c, everyone=False)
            return
        if goal == CLASS_RETURNED:
            if class_term is not None and class_term.returned_at is not None:
                return
            self._comments(s, c, everyone=True)
            self._submit(s, c)
            approvals.return_class(self._approver_ctx(s, c), c.plan.return_note)
            self._made(f"{c.cls.name} returned to its class teacher")
            return
        if goal == SHEET_RETURNED:
            subject = self._subject(s, c.plan.returned_sheet)
            if self._status(s, c, subject)[0] == SheetStatus.RETURNED:
                return
            self._comments(s, c, everyone=True)
            self._submit(s, c)
            approvals.return_class(
                self._approver_ctx(s, c), c.plan.return_note, subject_id=subject.pk,
            )
            self._made(f"{c.plan.returned_sheet} sheet of {c.cls.name} returned by the principal")
            return

        if stage != StoredStage.PUBLISHED:
            self._comments(s, c, everyone=True)
            self._submit(s, c)
            self._principal_comments(s, c)
            self._place_holds(s, c)
        if goal == PUBLISHED:
            if stage != StoredStage.PUBLISHED:
                counts = publishing.publish_class(self._approver_ctx(s, c))
                self._made(
                    f"{c.cls.name} published: {counts['released']} released, "
                    f"{counts['emailed']} emailed, {counts['held']} held",
                )
            self._lift_holds(s, c)


    # ── earlier terms and the cumulative card ─────────────────────────────

    def _skipped(self, text):
        """Say what was not reached and why; a line that is never counted as created."""
        self.stdout.write(f"  - {text}")

    def _earlier_terms(self, s, plan):
        """Bring the plan's class to published for each earlier term of the school year, oldest first.

        They go before the class's current term, because a current term that is
        the last of its year carries the year block only if the earlier terms are
        published when it is. A year's first term has none, so the previous year
        is looked at instead (:meth:`_previous_year`).
        """
        school_class, home = s.cast[plan.label]
        terms = cumulative.session_terms(s.tenant, s.term)
        earlier = terms[: [each.pk for each in terms].index(s.term.pk)]
        if not earlier:
            self._previous_year(s, school_class, home)
            return
        for term in earlier:
            roll = book.rolls_for(s.tenant, term, [(school_class, home)])[school_class.pk]
            if len(roll) < plan.min_roll:
                self._skipped(
                    f"{term.name} of {s.year.name} not published for {school_class.name}: "
                    f"{len(roll)} pupils were on its roll that term and a result needs "
                    f"at least {plan.min_roll} to show anything.",
                )
                return
            history = replace(
                plan, label=f"{plan.label}, {term.name}", goal=PUBLISHED, holds=(),
                cells={}, sheets={}, remind=(), uploads=(), corrections=(),
                history=False, drift=True, return_note="", returned_sheet="",
                sheet_notes={},
            )
            earlier_s = SimpleNamespace(**vars(s))
            earlier_s.term = term
            earlier_s.cast = {**s.cast, history.label: (school_class, home)}
            self._class(earlier_s, history)

    def _previous_year(self, s, school_class, home):
        """Say why nothing is published before the first term of the year, after looking at the year before.

        The previous year is inspected and never written to: a class there is
        published only if pupils were on its roll, and the student seeder
        places pupils in the active year alone.
        """
        previous = AcademicSession.objects.filter(
            tenant=s.tenant, start_date__lt=s.year.start_date,
        ).order_by("-start_date").first()
        if previous is None:
            self._skipped(
                f"no earlier term: {s.term.name} is the first term of {s.year.name}, "
                f"the school's first year.",
            )
            return
        last = AcademicTerm.objects.filter(tenant=s.tenant, session=previous).order_by(
            "order_index", "start_date", "pk",
        ).last()
        counterpart = SchoolClass.objects.filter(
            tenant=s.tenant, session=previous, name=school_class.name, is_active=True,
        ).select_related("branch").first()
        if last is None or counterpart is None:
            self._skipped(
                f"no earlier term: {s.term.name} is the first term of {s.year.name} and "
                f"{previous.name} has no {school_class.name} with a term to publish.",
            )
            return
        placed = resolve.class_branch(s.tenant, counterpart) or home
        pupils = len(book.rolls_for(s.tenant, last, [(counterpart, placed)])[counterpart.pk])
        status = previous.status.lower()
        reason = (
            "a class with nobody on its roll cannot be published"
            if not pupils
            else "earlier terms are published only within the current school year"
        )
        self._skipped(
            f"no earlier term: {s.term.name} is the first term of {s.year.name}. "
            f"{last.name} of {previous.name} ({status}) has {counterpart.name} with "
            f"{pupils} pupils on its roll, and {reason}, so term-on-term movers have "
            f"no data until a later term of {s.year.name}.",
        )

    def _cumulative_card(self, s, plan):
        """Say whether the class's current cards carry the year block, and why not when they do not."""
        school_class, _ = s.cast[plan.label]
        terms = cumulative.session_terms(s.tenant, s.term)
        if not cumulative.is_cumulative(terms, s.term):
            self._skipped(
                f"no cumulative card: {s.term.name} is not the last term of {s.year.name}.",
            )
            return
        carrying = StudentTermResult.objects.filter(
            tenant=s.tenant, class_term__school_class=school_class,
            class_term__term=s.term, snapshot__has_key="cumulative",
        ).exists()
        if carrying:
            self.stdout.write(
                f"  {school_class.name} cards for {s.term.name} carry the year block.",
            )
        else:
            self._skipped(
                f"no cumulative card: {school_class.name} was published for {s.term.name} "
                f"before its earlier terms were, so its cards carry no year block. A "
                f"published term is not rebuilt; start from a fresh database to see one.",
            )

    # ── corrections ───────────────────────────────────────────────────────

    def _raiser(self, s):
        from vs_user.models import User

        email = f"{RAISER}@{s.slug}.example.com"
        user = User.objects.filter(tenant=s.tenant, email=email).first()
        if user is None:
            raise CommandError(
                f"{s.slug} has no account {email}. Run seed_onboarding_scenarios first.",
            )
        if not has_permission(user, PERM_AMENDMENT_CREATE, tenant=s.tenant):
            raise CommandError(
                f"{email} cannot raise corrections. Run seed_all_permissions first.",
            )
        if not has_permission(s.admin, PERM_AMENDMENT_APPROVE, tenant=s.tenant):
            raise CommandError(
                f"{s.admin.email} cannot decide corrections. Run seed_all_permissions first.",
            )
        return user

    def _corrections(self, s, c):
        """Raise and decide the plan's corrections on the class's published cards, each once."""
        class_term = self._class_term(s, c)
        if class_term is None or class_term.stage != StoredStage.PUBLISHED:
            raise CommandError(f"{c.cls.name} is not published, so it has nothing to correct.")
        raiser = self._raiser(s)
        roll = self._roll(s, c)
        for fix in c.plan.corrections:
            student = roll[fix.index]
            subject = self._subject(s, fix.subject)
            found = Amendment.objects.filter(
                tenant=s.tenant, class_term=class_term, student=student, subject=subject,
                reason=fix.reason,
            ).first()
            if found is None:
                found = self._raise(s, c, raiser, student, subject, fix)
            if fix.decision != AmendmentStatus.PENDING and found.status == AmendmentStatus.PENDING:
                self._decide(s, c, found, fix)

    def _raise(self, s, c, raiser, student, subject, fix):
        target = amendments.open_target(
            s.tenant, raiser, c.cls.pk, s.term, student.pk, subject.pk,
        )
        form = amendments.form(target)
        column = next(
            (each for each in form["components"] if each["short_name"] == fix.component), None,
        )
        if column is None:
            raise CommandError(f"{subject.name} has no {fix.component} column to correct.")
        cell = next(each for each in form["cells"] if each["component"] == column["id"])
        if cell["marker"] or cell["mark"] is None:
            raise CommandError(
                f"{student.full_name}'s {fix.component} in {subject.name} is "
                f"{cell['marker'] or 'blank'}, which a correction by a number cannot replace.",
            )
        mark = cell["mark"] + fix.change
        if mark > column["max_score"]:
            mark = cell["mark"] - fix.change
        if not 0 <= mark <= column["max_score"] or mark == cell["mark"]:
            raise CommandError(
                f"A correction of {fix.change} to {student.full_name}'s {fix.component} "
                f"in {subject.name} ({cell['mark']} of {column['max_score']}) has nowhere to go.",
            )
        amendment = amendments.raise_amendment(
            target, fix.reason, [{"component": column["id"], "mark": mark, "marker": None}],
        )
        self._made(
            f"correction to {subject.name} for {student.full_name} ({c.cls.name}) "
            f"asked for by {raiser.full_name}",
        )
        return amendment

    def _decide(self, s, c, amendment, fix):
        """Approve or reject *amendment* as the school's administrator, who did not raise it."""
        result = StudentTermResult.objects.get(
            tenant=s.tenant, class_term=amendment.class_term, student=amendment.student,
        )
        decide = amendments.open_for_decision(s.tenant, s.admin, amendment.pk)
        if fix.decision == AmendmentStatus.APPROVED:
            if result.released_at is None:
                raise CommandError(
                    f"{amendment.student.full_name}'s result is held, so approving a "
                    f"correction to it would tell no guardian.",
                )
            amendments.approve(s.tenant, s.admin, decide, fix.note)
            self._made(
                f"correction to {amendment.subject.name} for {amendment.student.full_name} "
                f"approved by {s.admin.full_name}",
            )
        else:
            amendments.reject(s.tenant, s.admin, decide, fix.note)
            self._made(
                f"correction to {amendment.subject.name} for {amendment.student.full_name} "
                f"rejected by {s.admin.full_name}",
            )

    # ── a sheet part filled from a spreadsheet ────────────────────────────

    def _admission_numbers(self, s, students):
        """Give each pupil on a sheet an admission number where they have none.

        An upload matches a row to a pupil by admission number, and the student
        seeder leaves the number blank because a school that does not issue
        numbers automatically leaves it to the registrar. The numbers are
        written as the student record's own update writes one: judged by the
        branch's admission rule, checked free across the school, and audited.
        They run on from the highest of this school's own ``<initials>/<year>/``
        series in roll order, so a fresh database numbers the same pupils the
        same way.
        """
        blank = [student for student in students if not student.student_number]
        if not blank:
            return
        initials = "".join(word[0] for word in s.slug.split("-")).upper()
        head = f"{initials}/{s.year.start_date.year}/"
        used = [
            int(number[len(head):])
            for number in Student.objects.filter(
                tenant=s.tenant, student_number__startswith=head,
            ).values_list("student_number", flat=True)
            if number[len(head):].isdigit()
        ]
        following = max(used, default=0) + 1
        for student in blank:
            value = assert_number_allowed(
                s.tenant, f"{head}{following:04d}", branch=student.branch,
            )
            enrolment_service.assert_number_free(s.tenant, value, exclude_pk=student.pk)
            student.student_number = value
            student.save(update_fields=["student_number", "updated_at"])
            emit_audit_event(
                module_key=AuditModuleKey.STUDENT, action_type=AuditActionType.UPDATE,
                entity_type="Student", entity_id=str(student.pk),
                entity_label=student.full_name, tenant=s.tenant, actor_user=s.admin,
                summary=f"{student.full_name}'s record updated.",
            )
            following += 1
        self._made(
            f"admission numbers for {len(blank)} pupils "
            f"({blank[0].student_number} to {blank[-1].student_number})",
        )

    def _upload(self, s, c, subject_name):
        """Fill the end of an in-progress sheet from a spreadsheet the lead teacher checks and confirms.

        The workbook is the sheet's template with three changes: the marks of
        the :data:`UPLOAD_FILLS` pupils after the last fully entered one are
        written in, and the first pupil's first mark is moved by one, so the
        preview shows new, unchanged and replacing cells. The sheet stays in
        progress, with a pupil still blank. The upload is done once, judged by
        whether those pupils' marks are already on the sheet, so a rerun on a
        later day (when the uploaded file's record has expired) changes nothing.
        """
        subject = self._subject(s, subject_name)
        status, sheet = self._status(s, c, subject)
        label = f"{subject_name}, {c.cls.name}"
        lead = self._lead_user(s, c, subject)
        ctx = self._load(s, c, subject, lead, rule=sheets.ENTER)
        students = ctx.students
        if len(students) < BLANK_STUDENTS + UPLOAD_FILLS:
            raise CommandError(f"{label} has too few pupils to show an upload.")
        stored = (
            figures.stored_scores(s.tenant, [sheet.pk]).get(sheet.pk, {})
            if sheet is not None else {}
        )
        first = len(students) - BLANK_STUDENTS
        targets = students[first: first + UPLOAD_FILLS]
        if sheet is not None and all(
            (student.pk, component.pk) in stored
            for student in targets for component in ctx.components
        ):
            return
        if status != SheetStatus.IN_PROGRESS:
            raise CommandError(
                f"{label} is {status.lower()}, so it cannot be part filled from an upload.",
            )
        if not has_permission(lead, PERM_SHEET_IMPORT, tenant=s.tenant):
            raise CommandError(
                f"{lead.email} cannot upload scores. Run seed_all_permissions first.",
            )

        self._admission_numbers(s, students)
        rows = []
        for place, student in enumerate(students):
            cells = []
            for number, component in enumerate(ctx.components):
                mark, marker = stored.get((student.pk, component.pk), (None, None))
                if student in targets and mark is None and marker is None:
                    mark = generated_mark(student, subject, component)
                elif place == 0 and number == 0 and marker is None and mark is not None:
                    mark = mark + 1 if mark < component.max_score else mark - 1
                cells.append(marker or mark)
            rows.append((student.student_number, student.full_name, cells))
        workbook = spreadsheet.template_workbook(
            [f"{component.name} (max {component.max_score})" for component in ctx.components],
            rows,
        )

        upload = SimpleUploadedFile(
            importing.template_filename(ctx), workbook, content_type=XLSX,
        )
        found = importing.preview(ctx, upload)
        if found["file_issues"] or found["summary"]["with_issues"]:
            raise CommandError(
                f"The upload for {label} was refused: {found['file_issues'] or found['rows']}",
            )
        batch = importing.find(s.tenant, lead, found["token"], c.cls.pk, subject.pk)
        fresh = self._load(s, c, subject, lead, rule=sheets.ENTER)
        done = importing.confirm(fresh, batch, [row["row_number"] for row in found["rows"]])
        self._made(
            f"{label}: {done['scores']} scores entered from an uploaded spreadsheet "
            f"by {lead.full_name} ({done['replaced']} replacing earlier ones)",
        )

    # ── what the run leaves ───────────────────────────────────────────────

    def _summary(self, s):
        self.stdout.write("\n  Classes:")
        for plan in PLANS[s.slug]:
            school_class, home = s.cast[plan.label]
            entry = book.load(s.tenant, s.term, [(school_class, home)])[0]
            held = ResultHold.objects.filter(
                tenant=s.tenant, term=s.term, lifted_at__isnull=True,
                student_id__in=entry.roll_ids,
            ).count()
            self.stdout.write(
                f"    {school_class.name:8} {entry.stage:10} "
                f"{entry.subjects_in} of {entry.subjects_due} sheets in, "
                f"{len(entry.roll_ids)} pupils"
                f"{f', {held} held' if held else ''}"
                f"{', returned' if entry.returned else ''}",
            )
        counts = {
            status: Amendment.objects.filter(tenant=s.tenant, status=status).count()
            for status in AmendmentStatus.values
        }
        if any(counts.values()):
            self.stdout.write(
                "\n  Corrections: " + ", ".join(
                    f"{number} {status.lower()}" for status, number in counts.items()
                ),
            )
