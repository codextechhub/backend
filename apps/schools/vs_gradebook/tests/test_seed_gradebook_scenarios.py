"""``seed_gradebook_scenarios``: every state is reached, and a second run changes nothing.

The command hangs results off the schools the sibling seeders build, so this
class builds them the same way (permissions, notification catalogue, onboarding,
academic structure, students), with the school clock set to the day the dev
database was first seeded, then runs the gradebook seeder on a Monday six weeks
later. Both shapes of school are covered: Sunrise has one branch and Lagoon View
has two.

A second class runs the seeder late in the school year, on a day in the last
term, because that is the only day on which the earlier terms, the movers and the
cumulative card can be reached honestly: the first term has nothing before it.

Tagged ``slow`` because the sibling seeders and the permission seed cost
several seconds between them.
"""
from __future__ import annotations

import datetime as dt
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from urllib.parse import urlencode

from django.apps import apps
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, tag
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import AcademicSession, Department, SchoolClass, Subject
from schools.vs_gradebook.constants import (
    REASON_CANNOT_SIGN_IN,
    REASON_ENTERED,
    REASON_LAST_WORKING_DAY,
    REASON_NO_MAIN_TEACHER,
    AmendmentStatus,
    ClassStage,
    EmailState,
    ResultState,
    SetupStatus,
    SheetStatus,
    StoredStage,
)
from schools.vs_gradebook.models import (
    Amendment,
    AssessmentStructure,
    ClassTerm,
    GradingScale,
    ResultHold,
    Score,
    ScoreImport,
    ScoreSheet,
    StudentTermResult,
)
from schools.vs_gradebook.services import book
from schools.vs_gradebook.services.rules import read_gradebook_rules
from schools.vs_schools.dev.fixtures import build_school
from schools.vs_schools.models import School
from schools.vs_staff.constants import EmploymentStatus, TeachingPart
from schools.vs_staff.models import StaffProfile, TeachingAssignment
from schools.vs_students.models import Student
from vs_audit.models import AuditEvent
from vs_notifications.models import Notification
from vs_user.models import AccountLockout, User

from .base import school_clock

SEEDED_ON = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
MONDAY = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)
LAST_TERM_MONDAY = dt.datetime(2027, 4, 19, 12, 0, tzinfo=dt.timezone.utc)
SCHOOLS = {
    "sunrise-academy": ("Sunrise Academy", False),
    "lagoon-view": ("Lagoon View Academy", True),
}
SLUGS = tuple(SCHOOLS)


def _build(name, **options) -> None:
    """Run a prerequisite seeder with its chatter thrown away."""
    sink = StringIO()
    with redirect_stdout(sink):
        call_command(name, stdout=sink, stderr=sink, **options)


def _counts(tenant) -> dict:
    """Row counts of every gradebook model, and of what the seeder touches beside them."""
    out = {}
    for model in apps.get_app_config("vs_gradebook").get_models():
        manager = model._base_manager
        out[model.__name__] = (
            manager.filter(tenant=tenant).count()
            if any(f.name == "tenant" for f in model._meta.fields) else manager.count()
        )
    out.update({
        "students": Student.all_objects.filter(tenant=tenant).count(),
        "accounts": User.objects.filter(tenant=tenant).count(),
        "staff": StaffProfile.all_objects.filter(tenant=tenant).count(),
        "audit": AuditEvent.objects.filter(tenant=tenant).count(),
        "notifications": Notification.objects.filter(tenant=tenant).count(),
    })
    return out


class SeededSchools:
    """The two prerequisite schools with the seeder run once, on the day ``AT``.

    ``SLUGS`` names the schools built, and the first run's output, outgoing mail
    and row counts are kept so a test can compare a second run against them.
    """

    AT = MONDAY
    SLUGS = SLUGS
    ONLY = None

    @classmethod
    def setUpTestData(cls):
        with school_clock(SEEDED_ON):
            _build("seed_all_permissions")
            _build("seed_notification_event_types")
            _build("seed_notification_templates")
            for slug in cls.SLUGS:
                name, branched = SCHOOLS[slug]
                build_school(
                    slug=slug, name=name, live=True, extra_branch=branched,
                    with_onboarding=False,
                )
                _build("seed_academic_scenarios", only=slug)
                _build("seed_student_scenarios", only=slug)
        cls.tenants = {slug: School.objects.get(slug=slug).tenant for slug in cls.SLUGS}
        cls.first_run = cls.run_seed()
        cls.emails = list(mail.outbox)
        cls.after_first_run = {slug: _counts(t) for slug, t in cls.tenants.items()}

    @classmethod
    def run_seed(cls, at=None, **options) -> str:
        """Run the seeder on *at* (the class's day by default), with the emails it triggers sent as if committed."""
        out = StringIO()
        if cls.ONLY:
            options.setdefault("only", cls.ONLY)
        with school_clock(at or cls.AT), cls.captureOnCommitCallbacks(execute=True):
            call_command("seed_gradebook_scenarios", stdout=out, **options)
        return out.getvalue()

    # ── helpers ───────────────────────────────────────────────────────────

    def admin(self, slug):
        return User.objects.get(tenant=self.tenants[slug], email=f"admin@{slug}.example.com")

    def person(self, slug, first, last):
        return User.objects.get(
            tenant=self.tenants[slug], email=f"{first}.{last}@{slug}.example.com".lower(),
        )

    def term(self, slug):
        with school_clock(self.AT):
            from schools.vs_gradebook.services import resolve
            from vs_config.clock import tenant_today

            tenant = self.tenants[slug]
            return resolve.current_term(tenant, tenant_today(tenant))

    def klass(self, slug, name):
        return SchoolClass.objects.get(
            tenant=self.tenants[slug], name=name, is_active=True,
            session=self.term(slug).session,
        )

    def subject(self, slug, name):
        return Subject.all_objects.get(tenant=self.tenants[slug], name=name)

    def get(self, user, route, query=None, **kwargs):
        url = reverse(route, kwargs=kwargs)
        if query:
            url += "?" + urlencode(query)
        with school_clock(self.AT):
            response = TenantAPIClient(user).get(url)
        self.assertEqual(response.status_code, 200, getattr(response, "data", response))
        return response.data["data"]

    def sheet_state(self, slug, class_name, subject_name):
        found = ScoreSheet.objects.filter(
            class_term__school_class=self.klass(slug, class_name),
            class_term__term=self.term(slug), subject=self.subject(slug, subject_name),
        ).first()
        return found.status if found is not None else SheetStatus.NOT_STARTED

    def stage(self, slug, class_name):
        """The stage the results hub shows for the class."""
        hub = self.get(self.admin(slug), "gradebook-hub", {"term": self.term(slug).pk})
        return next(c["stage"] for c in hub["classes"] if c["name"] == class_name)


@tag("slow")
class SeedGradebookScenariosTests(SeededSchools, TestCase):
    # ── running it ────────────────────────────────────────────────────────

    def test_it_refuses_a_school_outside_the_cast(self):
        with self.assertRaises(CommandError):
            self.run_seed(only="holy-cross")

    def test_it_refuses_a_school_that_was_never_built(self):
        School.objects.filter(slug="sunrise-academy").update(slug="renamed")
        try:
            with self.assertRaises(CommandError):
                self.run_seed(only="sunrise-academy")
        finally:
            School.objects.filter(slug="renamed").update(slug="sunrise-academy")

    def test_a_second_run_creates_nothing_in_either_shape_of_school(self):
        self.assertNotIn("Created this run: 0", self.first_run)

        second = self.run_seed()

        self.assertIn("Created this run: 0", second)
        self.assertNotIn("  + ", second)
        for slug, tenant in self.tenants.items():
            with self.subTest(school=slug):
                self.assertEqual(_counts(tenant), self.after_first_run[slug])
        self.assertEqual(mail.outbox, [])

    def test_a_run_on_a_later_day_of_the_term_still_changes_nothing(self):
        tuesday = MONDAY + dt.timedelta(days=1)

        out = self.run_seed(at=tuesday)

        self.assertIn("Created this run: 0", out)
        for slug, tenant in self.tenants.items():
            with self.subTest(school=slug):
                self.assertEqual(_counts(tenant), self.after_first_run[slug])

    def test_one_school_can_be_seeded_alone(self):
        out = self.run_seed(only="lagoon-view")
        self.assertIn("Lagoon View Academy", out)
        self.assertNotIn("Sunrise Academy", out)
        self.assertIn("Created this run: 0", out)


    # ── setup ─────────────────────────────────────────────────────────────

    def in_use(self, model, slug):
        return {
            record.program.code: record
            for record in model.objects.filter(
                tenant=self.tenants[slug], status=SetupStatus.IN_USE,
            ).select_related("program")
        }

    def test_lagoon_view_grades_by_three_components_and_two_scales_and_moderates_seniors(self):
        slug = "lagoon-view"
        tenant = self.tenants[slug]
        structures = self.in_use(AssessmentStructure, slug)
        for code in ("JSS", "SSS"):
            self.assertEqual(
                [c.max_score for c in structures[code].components.all()], [20, 20, 60],
            )
        scales = self.in_use(GradingScale, slug)
        self.assertEqual(
            [b.grade for b in scales["JSS"].bands.order_by("-min_score")],
            ["A", "B", "C", "D", "F"],
        )
        self.assertEqual(
            [b.grade for b in scales["SSS"].bands.order_by("-min_score")],
            ["A1", "B2", "B3", "C4", "C5", "C6", "D7", "E8", "F9"],
        )
        rules = read_gradebook_rules(tenant)
        self.assertEqual(rules.moderated_programs, ("SSS",))
        self.assertTrue(
            rules.show_class_stats and rules.rank_in_class and rules.conduct_ratings
            and rules.rating_key,
        )
        headed = Department.all_objects.filter(tenant=tenant, head__isnull=False)
        self.assertEqual([d.name for d in headed], ["Sciences"])
        self.assertEqual(headed.get().head.user, self.person(slug, "oluwaseun", "bakare"))

    def test_sunrise_grades_by_two_components_and_one_scale_and_moderates_nothing(self):
        slug = "sunrise-academy"
        tenant = self.tenants[slug]
        for code, structure in self.in_use(AssessmentStructure, slug).items():
            self.assertEqual([c.max_score for c in structure.components.all()], [40, 60], code)
        for code, scale in self.in_use(GradingScale, slug).items():
            self.assertEqual(
                [b.grade for b in scale.bands.order_by("-min_score")],
                ["A", "B", "C", "D", "F"], code,
            )
        rules = read_gradebook_rules(tenant)
        self.assertEqual(rules.moderated_programs, ())
        self.assertFalse(rules.rank_in_class or rules.conduct_ratings or rules.rating_key)
        self.assertFalse(Department.all_objects.filter(tenant=tenant, head__isnull=False).exists())
        self.assertFalse(ScoreSheet.objects.filter(tenant=tenant, needs_moderation=True).exists())

    # ── sheets ────────────────────────────────────────────────────────────

    def test_every_sheet_state_is_reached_at_lagoon_view(self):
        slug = "lagoon-view"
        tenant, term = self.tenants[slug], self.term(slug)
        hub = self.get(self.admin(slug), "gradebook-hub", {"term": term.pk})

        late = {(row["class"]["name"], row["subject"]["name"]): row for row in hub["late"]}
        statuses = {row["status"] for row in late.values()}
        self.assertEqual(
            statuses,
            {SheetStatus.NOT_STARTED, SheetStatus.IN_PROGRESS, SheetStatus.SUBMITTED,
             SheetStatus.RETURNED},
        )
        in_progress = late[("SSS1 B", "English Language")]
        self.assertLess(in_progress["entered"], in_progress["roll"])
        self.assertTrue(in_progress["reminded_today"])
        self.assertEqual(late[("SSS1 B", "Mathematics")]["reason_kind"], "AWAITING_MODERATION")
        self.assertEqual({row["reason_kind"] for row in late.values() if row["status"] == "RETURNED"},
                         {"RETURNED"})
        self.assertFalse(late[("SSS1 B", "Civic Education")]["reminded_today"])

        stored = set(ScoreSheet.objects.filter(tenant=tenant).values_list("status", flat=True))
        self.assertTrue(
            {SheetStatus.APPROVED, SheetStatus.SUBMITTED, SheetStatus.RETURNED,
             SheetStatus.IN_PROGRESS} <= stored,
        )

    def test_the_moderation_queue_has_a_sheet_to_decide_and_one_decided_each_way(self):
        slug = "lagoon-view"
        term = self.term(slug)
        head = self.person(slug, "oluwaseun", "bakare")
        queue = self.get(head, "gradebook-moderation", {"term": term.pk})["sheets"]

        by_status = {}
        for row in queue:
            by_status.setdefault(row["status"], []).append(row)
        self.assertEqual(set(by_status), {"SUBMITTED", "APPROVED", "RETURNED"})
        self.assertTrue(all(row["can_decide"] for row in by_status["SUBMITTED"]))
        self.assertFalse(any(row["can_decide"] for row in by_status["APPROVED"] + by_status["RETURNED"]))
        self.assertEqual({row["subject"]["name"] for row in queue}, {"Mathematics", "Further Mathematics"})
        self.assertTrue(all(row["class"]["name"].startswith("SSS") for row in queue))

        duties = self.get(head, "gradebook-hub-mine", {"term": term.pk})["duties"]
        [duty] = [d for d in duties if d["kind"] == "HEAD_OF_DEPARTMENT"]
        self.assertEqual((duty["department"]["name"], duty["waiting"]), ("Sciences", 1))

    def test_a_returned_sheet_carries_the_heads_name_and_note(self):
        slug = "lagoon-view"
        term = self.term(slug)
        returned = ScoreSheet.objects.get(
            tenant=self.tenants[slug], status=SheetStatus.RETURNED,
            returned_by=self.person(slug, "oluwaseun", "bakare"),
        )
        teacher = self.person(slug, "kayode", "adeniyi")
        body = self.get(
            teacher, "gradebook-sheet", {"term": term.pk},
            class_id=returned.class_term.school_class_id, subject_id=returned.subject_id,
        )
        self.assertEqual(body["status"], "RETURNED")
        self.assertEqual(body["returned"]["by"], "Oluwaseun Bakare")
        self.assertTrue(body["returned"]["note"])
        self.assertTrue(body["can_enter"])

    def test_an_optional_subjects_sheet_lists_only_the_pupils_who_take_it(self):
        slug = "lagoon-view"
        term = self.term(slug)
        awaiting = ClassTerm.objects.get(
            tenant=self.tenants[slug], term=term, stage=StoredStage.AWAITING,
        )
        body = self.get(
            self.admin(slug), "gradebook-sheet", {"term": term.pk},
            class_id=awaiting.school_class_id, subject_id=self.subject(slug, "Economics").pk,
        )
        self.assertTrue(body["is_elective"])
        self.assertGreater(body["takers"], 0)
        self.assertLess(body["takers"], body["roll"])
        self.assertEqual(len(body["rows"]), body["takers"])

    def test_an_excused_component_and_an_absent_cell_are_on_a_published_sheet(self):
        slug = "lagoon-view"
        term = self.term(slug)
        published = ClassTerm.objects.get(
            tenant=self.tenants[slug], term=term, stage=StoredStage.PUBLISHED,
        )
        maths = self.get(
            self.admin(slug), "gradebook-sheet", {"term": term.pk},
            class_id=published.school_class_id, subject_id=self.subject(slug, "Mathematics").pk,
        )
        notes = [row["excused_note"] for row in maths["rows"] if row["excused_note"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("out of 80 obtainable", notes[0])

        civic = self.get(
            self.admin(slug), "gradebook-sheet", {"term": term.pk},
            class_id=published.school_class_id, subject_id=self.subject(slug, "Civic Education").pk,
        )
        markers = [c["marker"] for row in civic["rows"] for c in row["cells"] if c["marker"]]
        self.assertEqual(markers, ["ABS"])
        self.assertTrue(Score.objects.filter(tenant=self.tenants[slug], marker="EXC").exists())

    def test_an_optional_subject_is_taken_by_part_of_the_class_at_lagoon_view_only(self):
        self.assertFalse(
            Subject.all_objects.filter(tenant=self.tenants["sunrise-academy"], name="Yoruba").exists(),
        )
        from schools.vs_academics.models import ElectiveTaker

        taken = {
            row["subject__name"]
            for row in ElectiveTaker.objects.filter(tenant=self.tenants["lagoon-view"])
            .values("subject__name").distinct()
        }
        self.assertEqual(taken, {"Yoruba", "Further Mathematics", "Economics"})

    # ── classes ───────────────────────────────────────────────────────────

    def test_every_class_stage_is_reached_at_lagoon_view(self):
        slug = "lagoon-view"
        hub = self.get(self.admin(slug), "gradebook-hub", {"term": self.term(slug).pk})

        stages = {}
        for row in hub["classes"]:
            stages.setdefault(row["stage"], []).append(row)
        self.assertEqual(
            set(stages),
            {ClassStage.ENTERING, ClassStage.COMPILING, ClassStage.AWAITING, ClassStage.PUBLISHED},
        )
        self.assertEqual(
            (hub["kpis"]["published"], hub["kpis"]["awaiting_approval"], hub["kpis"]["held"]),
            (1, 1, 2),
        )
        self.assertEqual([r["name"] for r in stages[ClassStage.COMPILING] if r["returned"]], ["JSS2 A"])
        self.assertTrue(hub["branch_dimension"])
        self.assertEqual(
            {row["branch"]["name"] for row in hub["classes"]},
            {b.name for b in self.tenants[slug].branches.all()},
        )

    def test_a_compiling_class_has_its_comments_partly_written(self):
        slug = "lagoon-view"
        term = self.term(slug)
        compiling = next(
            c for c in self.get(self.admin(slug), "gradebook-hub", {"term": term.pk})["classes"]
            if c["stage"] == ClassStage.COMPILING and not c["returned"]
        )
        class_term = ClassTerm.objects.get(
            tenant=self.tenants[slug], term=term, school_class_id=compiling["id"],
        )
        written = StudentTermResult.objects.filter(class_term=class_term).exclude(teacher_comment="")
        roll = self.get(
            self.admin(slug), "gradebook-broadsheet", {"term": term.pk}, class_id=compiling["id"],
        )
        self.assertTrue(0 < written.count() < len(roll["rows"]))
        self.assertFalse(roll["can_submit"])
        self.assertEqual(compiling["subjects_in"], compiling["subjects_due"])

    def test_a_class_returned_by_the_principal_stands_with_her_note(self):
        slug = "lagoon-view"
        term = self.term(slug)
        returned = ClassTerm.objects.get(
            tenant=self.tenants[slug], term=term, stage__isnull=True,
            returned_at__isnull=False,
        )
        sheet = self.get(
            self.admin(slug), "gradebook-broadsheet", {"term": term.pk},
            class_id=returned.school_class_id,
        )
        self.assertEqual(sheet["returned"]["note"], returned.returned_note)
        self.assertTrue(sheet["returned"]["note"])

    def test_an_awaiting_class_has_the_principals_comments_and_a_held_student(self):
        slug = "lagoon-view"
        term = self.term(slug)
        admin = self.admin(slug)
        queue = self.get(admin, "gradebook-approvals", {"term": term.pk})["classes"]
        awaiting = next(row for row in queue if row["stage"] == "AWAITING")
        self.assertTrue(awaiting["can_decide"])
        self.assertEqual(len(awaiting["held"]), 1)
        self.assertEqual(awaiting["held"][0]["held_by"], "Ngozi Ibekwe")

        comments = self.get(
            admin, "gradebook-principal-comments", {"term": term.pk},
            class_id=awaiting["class"]["id"],
        )
        self.assertFalse(comments["locked"])
        self.assertTrue(all(band["override"] for band in comments["bands"]))

        preview = self.get(
            admin, "gradebook-publish-preview", {"term": term.pk}, class_id=awaiting["class"]["id"],
        )
        self.assertEqual(len(preview["held"]), 1)
        roll = self.get(
            admin, "gradebook-broadsheet", {"term": term.pk}, class_id=awaiting["class"]["id"],
        )["rows"]
        self.assertEqual(preview["released"] + len(preview["held"]), len(roll))

    def test_a_class_with_one_sheet_returned_by_the_principal_is_entering_again(self):
        slug = "lagoon-view"
        term = self.term(slug)
        principal = self.admin(slug)
        sheet = ScoreSheet.objects.get(
            tenant=self.tenants[slug], status=SheetStatus.RETURNED, returned_by=principal,
        )
        class_term = sheet.class_term
        self.assertIsNone(class_term.stage)
        self.assertEqual(class_term.returned_note, "")
        late = self.get(principal, "gradebook-hub", {"term": term.pk})["late"]
        row = next(r for r in late if r["subject"]["id"] == sheet.subject_id
                   and r["class"]["id"] == class_term.school_class_id)
        self.assertEqual(row["reason"], f"Returned by {principal.full_name}")

    # ── publication and holds ─────────────────────────────────────────────

    def test_the_published_class_released_all_but_one_and_emailed_the_guardians(self):
        slug = "lagoon-view"
        tenant, term = self.tenants[slug], self.term(slug)
        published = ClassTerm.objects.get(tenant=tenant, term=term, stage=StoredStage.PUBLISHED)
        body = self.get(
            self.admin(slug), "gradebook-release", {"term": term.pk},
            class_id=published.school_class_id,
        )

        states = {}
        for row in body["rows"]:
            states.setdefault(row["result_state"], []).append(row)
        self.assertEqual(set(states), {ResultState.RELEASED, ResultState.HELD})
        self.assertEqual(len(states[ResultState.HELD]), 1)
        self.assertEqual(body["held_count"], 1)
        self.assertEqual(states[ResultState.HELD][0]["email_state"], EmailState.HELD)
        self.assertIsNone(states[ResultState.HELD][0]["emailed_at"])
        self.assertTrue(all(r["email_state"] == EmailState.SENT for r in states[ResultState.RELEASED]))
        self.assertEqual(len(states[ResultState.RELEASED]) + 1, body["class_size"])

        to_guardians = [m for m in self.emails if "household@example.ng" in ",".join(m.to)]
        self.assertGreaterEqual(len(to_guardians), len(states[ResultState.RELEASED]))

    def test_a_published_card_renders_with_position_ratings_and_both_comments(self):
        slug = "lagoon-view"
        tenant, term = self.tenants[slug], self.term(slug)
        published = ClassTerm.objects.get(tenant=tenant, term=term, stage=StoredStage.PUBLISHED)
        result = StudentTermResult.objects.filter(
            class_term=published, released_at__isnull=False,
        ).first()
        card = self.get(
            self.admin(slug), "gradebook-report-card", {"term": term.pk},
            class_id=published.school_class_id, student_id=result.student_id,
        )
        self.assertFalse(card["held"])
        labels = {line["label"] for line in card["totals"]}
        self.assertIn("Position in class", labels)
        self.assertTrue(card["ratings"] and card["rating_key"])
        self.assertTrue(card["teacher_comment"] and card["principal_comment"])
        self.assertEqual([s["name"] for s in card["subjects"]],
                         ["Basic Science", "Civic Education", "English Language", "Mathematics"])
        self.assertTrue(all(s["grade"] for s in card["subjects"] if s["total"] is not None))
        results = self.get(self.admin(slug), "gradebook-student-results", student_id=result.student_id)
        [entry] = results["terms"]
        self.assertTrue(entry["published"] and entry["position"])

    def test_the_held_student_has_no_card_and_a_reason_and_the_lifted_one_was_released(self):
        slug = "lagoon-view"
        tenant, term = self.tenants[slug], self.term(slug)
        bursar = self.person(slug, "ngozi", "ibekwe")
        rows = self.get(bursar, "gradebook-holds", {"term": term.pk, "page_size": 100})
        held = [row for row in rows if row["hold"]]
        self.assertEqual(len(held), 2)
        self.assertTrue(all(row["hold"]["reason"] and row["hold"]["placed_by"] == "Ngozi Ibekwe"
                            for row in held))

        lifted = ResultHold.objects.get(tenant=tenant, term=term, lifted_at__isnull=False)
        self.assertEqual(lifted.lift_note, "Cheque cleared; paid in full at the bursary.")
        self.assertEqual(lifted.lifted_by, bursar)
        result = StudentTermResult.objects.get(student=lifted.student, class_term__term=term)
        self.assertEqual(result.email_state, EmailState.SENT)
        self.assertIsNotNone(result.released_at)
        listed = next(row for row in rows if row["student"]["id"] == lifted.student_id)
        self.assertIsNone(listed["hold"])
        self.assertTrue(listed["released"])

        still_held = ResultHold.objects.get(tenant=tenant, term=term, lifted_at__isnull=True,
                                            student__in=[r["student"]["id"] for r in held
                                                         if r["class"]["name"] == listed["class"]["name"]])
        held_result = StudentTermResult.objects.get(student=still_held.student, class_term__term=term)
        self.assertIsNone(held_result.released_at)
        self.assertEqual(held_result.email_state, EmailState.HELD)

    def test_no_guardian_without_an_address_exists_until_the_attendance_seeder_adds_one(self):
        self.assertFalse(
            StudentTermResult.objects.filter(email_state=EmailState.NO_EMAIL).exists(),
        )

    # ── the school with one branch ────────────────────────────────────────

    def test_sunrise_has_a_published_class_with_no_position_and_one_still_entering(self):
        slug = "sunrise-academy"
        tenant, term = self.tenants[slug], self.term(slug)
        admin = self.admin(slug)
        hub = self.get(admin, "gradebook-hub", {"term": term.pk})

        self.assertFalse(hub["branch_dimension"])
        self.assertTrue(all(row["branch"] is None for row in hub["classes"]))
        by_stage = {row["stage"]: row for row in hub["classes"] if row["subjects_due"]}
        self.assertEqual(set(by_stage), {ClassStage.PUBLISHED, ClassStage.ENTERING})
        self.assertEqual(
            {row["status"] for row in hub["late"]},
            {SheetStatus.NOT_STARTED, SheetStatus.IN_PROGRESS},
        )

        published = ClassTerm.objects.get(tenant=tenant, term=term, stage=StoredStage.PUBLISHED)
        result = StudentTermResult.objects.filter(
            class_term=published, released_at__isnull=False,
        ).first()
        card = self.get(
            admin, "gradebook-report-card", {"term": term.pk},
            class_id=published.school_class_id, student_id=result.student_id,
        )
        self.assertEqual(
            {line["label"] for line in card["totals"]},
            {"Total obtained", "Total obtainable", "Overall average"},
        )
        self.assertFalse(card.get("ratings"))
        self.assertTrue(card["principal_comment"])
        self.assertEqual(
            [c["max_score"] for c in card["components"]], [40, 60],
        )
        self.assertFalse(ResultHold.objects.filter(tenant=tenant).exists())
        self.assertEqual(
            self.get(admin, "gradebook-moderation", {"term": term.pk})["sheets"], [],
        )
        [entry] = self.get(admin, "gradebook-student-results", student_id=result.student_id)["terms"]
        self.assertTrue(entry["published"])
        self.assertNotIn("position", entry)

    # ── late reasons that name a person ───────────────────────────────────

    def late_reasons_class(self, slug="sunrise-academy"):
        """The class whose Mathematics the resigning teacher leads."""
        lead = TeachingAssignment.objects.get(
            tenant=self.tenants[slug], part=TeachingPart.LEAD,
            staff__user=self.person(slug, "yetunde", "ogunleye"),
            subject=self.subject(slug, "Mathematics"),
        )
        return lead.school_class

    def late_rows(self, slug="sunrise-academy"):
        hub = self.get(self.admin(slug), "gradebook-hub", {"term": self.term(slug).pk})
        name = self.late_reasons_class(slug).name
        return hub, {
            row["subject"]["name"]: row for row in hub["late"] if row["class"]["name"] == name
        }

    def test_the_late_list_carries_all_three_reasons_that_name_a_person(self):
        hub, rows = self.late_rows()

        self.assertEqual(self.late_reasons_class().name, "JSS2 A")
        self.assertTrue(
            {REASON_LAST_WORKING_DAY, REASON_CANNOT_SIGN_IN, REASON_NO_MAIN_TEACHER}
            <= {row["reason_kind"] for row in hub["late"]},
        )
        self.assertEqual(
            {name: row["reason_kind"] for name, row in rows.items()},
            {
                "Mathematics": REASON_LAST_WORKING_DAY,
                "English Language": REASON_CANNOT_SIGN_IN,
                "Civic Education": REASON_NO_MAIN_TEACHER,
            },
        )
        stage = next(c for c in hub["classes"] if c["name"] == "JSS2 A")
        self.assertEqual((stage["stage"], stage["subjects_in"], stage["subjects_due"]),
                         (ClassStage.ENTERING, 1, 4))

    def test_the_last_working_day_is_a_resignation_inside_the_term(self):
        slug = "sunrise-academy"
        _, rows = self.late_rows(slug)
        row = rows["Mathematics"]
        teacher = StaffProfile.all_objects.get(user=self.person(slug, "yetunde", "ogunleye"))
        term = self.term(slug)

        self.assertEqual(row["main_teacher"]["name"], "Yetunde Ogunleye")
        self.assertEqual(row["status"], SheetStatus.IN_PROGRESS)
        self.assertTrue(0 < row["entered"] < row["roll"])
        self.assertTrue(row["reason"].startswith("Last working day "))
        self.assertIsNone(row["staff_link"])
        self.assertEqual(teacher.employment_status, EmploymentStatus.RESIGNED)
        self.assertTrue(MONDAY.date() < teacher.exit_date <= term.end_date)
        self.assertTrue(teacher.user.may_sign_in)

    def test_cannot_sign_in_is_a_suspended_account_with_a_link_to_the_staff_record(self):
        slug = "sunrise-academy"
        _, rows = self.late_rows(slug)
        row = rows["English Language"]
        teacher = StaffProfile.all_objects.get(user=self.person(slug, "emmanuel", "okon"))

        self.assertEqual(row["status"], SheetStatus.NOT_STARTED)
        self.assertEqual(row["reason"], "Emmanuel Okon cannot sign in")
        self.assertEqual(row["staff_link"], teacher.pk)
        self.assertEqual(teacher.employment_status, EmploymentStatus.SUSPENDED)
        self.assertIsNone(teacher.exit_date)
        self.assertEqual(teacher.user.status, User.Status.SUSPENDED)
        self.assertFalse(teacher.user.may_sign_in)
        self.assertFalse(AccountLockout.objects.filter(user=teacher.user).exists())

    def test_a_reader_who_may_not_reactivate_accounts_sees_the_entered_count_instead(self):
        from schools.vs_gradebook.services import hubs

        slug = "sunrise-academy"
        name = self.late_reasons_class(slug).name
        with school_clock(MONDAY):
            body = hubs.admin_hub(
                self.tenants[slug], self.admin(slug), self.term(slug), None,
                show_locked_accounts=False,
            )
        row = next(
            r for r in body["late"]
            if r["class"]["name"] == name and r["subject"]["name"] == "English Language"
        )
        self.assertEqual(row["reason_kind"], REASON_ENTERED)
        self.assertIsNone(row["staff_link"])

    def test_no_main_teacher_is_a_due_sheet_nobody_leads(self):
        slug = "sunrise-academy"
        _, rows = self.late_rows(slug)
        row = rows["Civic Education"]

        self.assertIsNone(row["main_teacher"])
        self.assertEqual(row["reason"], "No main teacher assigned")
        self.assertFalse(
            TeachingAssignment.objects.filter(
                tenant=self.tenants[slug], school_class=self.late_reasons_class(slug),
                subject=self.subject(slug, "Civic Education"), part=TeachingPart.LEAD,
            ).exists(),
        )

    def test_a_report_card_email_says_the_card_is_ready_and_nothing_of_the_result(self):
        ready = [m for m in self.emails if "report card" in (m.subject + m.body).lower()]
        self.assertTrue(ready)
        for message in ready:
            body = message.body.lower()
            for forbidden in ("position", "average", "grade", "%"):
                self.assertNotIn(forbidden, body, message.subject)

    # ── corrections to a published card ───────────────────────────────────

    def amendment_rows(self, user=None, slug="lagoon-view"):
        return self.get(
            user or self.admin(slug), "gradebook-amendments",
            {"term": self.term(slug).pk, "page_size": 100},
        )

    def test_the_amendments_list_has_one_waiting_one_approved_and_one_rejected(self):
        slug = "lagoon-view"
        rows = self.amendment_rows()

        self.assertEqual(
            sorted(row["status"] for row in rows),
            [AmendmentStatus.APPROVED, AmendmentStatus.PENDING, AmendmentStatus.REJECTED],
        )
        self.assertEqual(rows[0]["status"], AmendmentStatus.PENDING)
        by_status = {row["status"]: row for row in rows}
        self.assertEqual({row["raised_by"] for row in rows}, {"Chukwuemeka Eze"})
        principal = self.admin(slug).full_name
        self.assertNotEqual(principal, "Chukwuemeka Eze")

        waiting = by_status[AmendmentStatus.PENDING]
        self.assertIsNone(waiting["decided_by"])
        self.assertIsNone(waiting["version"])
        self.assertTrue(waiting["amendment_reason"])

        approved = by_status[AmendmentStatus.APPROVED]
        self.assertEqual((approved["decided_by"], approved["version"]), (principal, 2))
        self.assertNotEqual(approved["before"]["total"], approved["after"]["total"])

        rejected = by_status[AmendmentStatus.REJECTED]
        self.assertEqual(rejected["decided_by"], principal)
        self.assertTrue(rejected["decision_note"])
        self.assertIsNone(rejected["version"])
        self.assertNotEqual(rejected["before"]["total"], rejected["after"]["total"])

        branch_admin = User.objects.get(
            tenant=self.tenants[slug], email=f"branch.admin@{slug}.example.com",
        )
        self.assertEqual(len(self.amendment_rows(branch_admin)), 3)

    def test_the_corrections_are_for_released_pupils_of_the_published_class_in_one_branch(self):
        slug = "lagoon-view"
        tenant, term = self.tenants[slug], self.term(slug)
        published = ClassTerm.objects.get(tenant=tenant, term=term, stage=StoredStage.PUBLISHED)
        found = Amendment.objects.filter(tenant=tenant)

        self.assertEqual(found.count(), 3)
        self.assertEqual({a.class_term_id for a in found}, {published.pk})
        self.assertEqual(len({a.student_id for a in found}), 3)
        for amendment in found:
            result = StudentTermResult.objects.get(
                class_term=published, student=amendment.student,
            )
            self.assertIsNotNone(result.released_at, amendment.student)
            self.assertNotEqual(amendment.raised_by_id, amendment.decided_by_id)

    def test_an_approved_correction_reissued_one_card_and_left_the_others_alone(self):
        slug = "lagoon-view"
        tenant, term = self.tenants[slug], self.term(slug)
        approved = Amendment.objects.get(tenant=tenant, status=AmendmentStatus.APPROVED)
        card = self.get(
            self.admin(slug), "gradebook-report-card", {"term": term.pk},
            class_id=approved.class_term.school_class_id, student_id=approved.student_id,
        )
        self.assertEqual(card["version"], 2)
        untouched = StudentTermResult.objects.filter(
            class_term=approved.class_term, snapshot__isnull=False,
        ).exclude(student=approved.student)
        self.assertEqual({result.version for result in untouched}, {1})

        amended = Score.objects.filter(
            tenant=tenant, sheet__class_term=approved.class_term, amended_by__isnull=False,
        )
        self.assertEqual({score.student_id for score in amended}, {approved.student_id})
        self.assertEqual({score.amended_by.email for score in amended},
                         {f"branch.admin@{slug}.example.com"})

    def test_a_correction_that_waits_or_was_rejected_changed_no_card_and_wrote_to_nobody(self):
        tenant = self.tenants["lagoon-view"]
        for amendment in Amendment.objects.filter(tenant=tenant).exclude(
            status=AmendmentStatus.APPROVED,
        ):
            result = StudentTermResult.objects.get(
                class_term=amendment.class_term, student=amendment.student,
            )
            self.assertEqual(result.version, 1, amendment.status)
        updated = [m for m in self.emails if "updated report card is ready" in m.subject]
        approved = Amendment.objects.get(tenant=tenant, status=AmendmentStatus.APPROVED)
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0].subject, f"{approved.student.first_name}'s updated report card is ready")
        self.assertIn("household@example.ng", ",".join(updated[0].to))
        self.assertTrue(
            Notification.objects.filter(
                tenant=tenant, event_type__key="gradebook.report_card_updated",
            ).exists(),
        )

    def test_the_students_history_tells_of_all_three_corrections(self):
        slug = "lagoon-view"
        tenant = self.tenants[slug]
        approved = Amendment.objects.get(tenant=tenant, status=AmendmentStatus.APPROVED)
        results = self.get(
            self.admin(slug), "gradebook-student-results", student_id=approved.student_id,
        )
        entries = [e for e in results["history"] if e["kind"] == "AMENDMENT"]
        self.assertEqual([e["amendment"]["status"] for e in entries], [AmendmentStatus.APPROVED])

    def test_sunrise_has_no_corrections(self):
        self.assertFalse(Amendment.objects.filter(tenant=self.tenants["sunrise-academy"]).exists())

    # ── a sheet part filled from a spreadsheet ────────────────────────────

    def test_part_of_a_sheet_was_entered_from_a_checked_and_confirmed_upload(self):
        slug = "lagoon-view"
        tenant, term = self.tenants[slug], self.term(slug)
        english = self.subject(slug, "English Language")
        sheet = ScoreSheet.objects.get(
            tenant=tenant, class_term__school_class=self.klass(slug, "SSS1 B"),
            class_term__term=term, subject=english,
        )
        upload = ScoreImport.objects.get(
            tenant=tenant, school_class=sheet.class_term.school_class, subject=english, term=term,
        )

        self.assertIsNotNone(upload.confirmed_at)
        self.assertEqual(upload.user, self.person(slug, "hauwa", "sanni"))
        outcomes = {
            cell["outcome"] for row in upload.rows for cell in row["cells"]
        }
        self.assertTrue({"NEW", "SAME", "REPLACES"} <= outcomes, outcomes)
        self.assertTrue(all(not row["issues"] for row in upload.rows))
        self.assertEqual(sheet.status, SheetStatus.IN_PROGRESS)
        self.assertTrue(
            AuditEvent.objects.filter(
                tenant=tenant, summary__contains="from a spreadsheet",
            ).exists(),
        )

        body = self.get(
            self.admin(slug), "gradebook-sheet", {"term": term.pk},
            class_id=sheet.class_term.school_class_id, subject_id=english.pk,
        )
        complete = [
            row for row in body["rows"]
            if all(cell["mark"] is not None for cell in row["cells"])
        ]
        empty = [
            row for row in body["rows"]
            if all(cell["mark"] is None for cell in row["cells"])
        ]
        self.assertGreater(len(complete), 0)
        self.assertEqual(len(empty), 1)
        self.assertEqual(len(complete) + len(empty), len(body["rows"]))

    # ── what the first term of the year cannot have ───────────────────────

    def test_the_first_term_says_why_it_has_no_earlier_term_and_no_cumulative_card(self):
        self.assertIn("no earlier term: First Term is the first term of 2026/2027", self.first_run)
        self.assertIn("2025/2026 (archived)", self.first_run)
        self.assertIn("0 pupils on its roll", self.first_run)
        self.assertIn("no cumulative card: First Term is not the last term", self.first_run)
        slug = "lagoon-view"
        report = self.get(
            self.admin(slug), "gradebook-analytics", {"term": self.term(slug).pk},
        )
        self.assertEqual(report["movers"], [])
        self.assertTrue(report["subjects"])

    def test_nothing_is_published_for_a_term_other_than_the_current_one(self):
        slug = "lagoon-view"
        tenant = self.tenants[slug]
        self.assertEqual(
            set(ClassTerm.objects.filter(tenant=tenant, stage=StoredStage.PUBLISHED)
                .values_list("term_id", flat=True)),
            {self.term(slug).pk},
        )


@tag("slow")
class SeedGradebookScenariosLateInTheYearTests(SeededSchools, TestCase):
    """Run in the last term of the year, where the earlier terms and the year block can be reached."""

    AT = LAST_TERM_MONDAY
    SLUGS = ("lagoon-view",)
    ONLY = "lagoon-view"
    SLUG = "lagoon-view"

    def published_class(self):
        return self.klass(self.SLUG, "JSS1 A")

    def test_the_day_is_in_the_third_term(self):
        self.assertEqual(self.term(self.SLUG).order_index, 3)
        self.assertNotIn("no earlier term", self.first_run)
        self.assertNotIn("no cumulative card", self.first_run)

    def test_every_term_of_the_year_is_published_for_the_class_in_order(self):
        tenant, term = self.tenants[self.SLUG], self.term(self.SLUG)
        terms = list(term.session.terms.order_by("order_index"))
        self.assertEqual(len(terms), 3)
        published = {
            each.term_id: each
            for each in ClassTerm.objects.filter(
                tenant=tenant, school_class=self.published_class(), stage=StoredStage.PUBLISHED,
            )
        }
        self.assertEqual(set(published), {each.pk for each in terms})
        order = [published[each.pk].published_at for each in terms]
        self.assertEqual(order, sorted(order))
        for each in terms[:-1]:
            self.assertEqual(
                {result.version for result in StudentTermResult.objects.filter(
                    class_term=published[each.pk], snapshot__isnull=False,
                )} - {1}, set(),
            )

    def test_the_movers_include_pupils_who_rose_and_pupils_who_fell(self):
        term = self.term(self.SLUG)
        report = self.get(self.admin(self.SLUG), "gradebook-analytics", {"term": term.pk})

        movers = report["movers"]
        self.assertGreater(len(movers), 2)
        signs = {row["change"][0] for row in movers}
        self.assertEqual(signs, {"+", "-"})
        second = term.session.terms.get(order_index=2)
        self.assertEqual({row["from_term"]["id"] for row in movers}, {second.pk})
        self.assertEqual({row["class"]["name"] for row in movers}, {"JSS1 A"})
        falls = [Decimal(row["change"]) for row in movers]
        self.assertEqual(falls, sorted(falls))

    def test_a_cumulative_card_renders_with_all_three_terms_and_an_annual_position(self):
        tenant, term = self.tenants[self.SLUG], self.term(self.SLUG)
        published = ClassTerm.objects.get(
            tenant=tenant, term=term, school_class=self.published_class(),
            stage=StoredStage.PUBLISHED,
        )
        result = StudentTermResult.objects.filter(
            class_term=published, released_at__isnull=False,
        ).exclude(student__in=Amendment.objects.values("student")).first()
        card = self.get(
            self.admin(self.SLUG), "gradebook-report-card", {"term": term.pk},
            class_id=published.school_class_id, student_id=result.student_id,
        )

        block = card["cumulative"]
        self.assertEqual(len(block["terms"]), 3)
        self.assertTrue(block["annual_average"])
        self.assertTrue(block["annual_position"])
        self.assertEqual(
            block["terms"], [each.name for each in term.session.terms.order_by("order_index")],
        )
        for subject in block["subjects"]:
            self.assertEqual(len(subject["totals"]), 3)
            self.assertTrue(all(total for total in subject["totals"]), subject)
        results = self.get(self.admin(self.SLUG), "gradebook-student-results", student_id=result.student_id)
        self.assertEqual(len(results["terms"]), 3)

    def test_a_correction_to_the_third_term_keeps_the_year_block_on_the_new_version(self):
        tenant = self.tenants[self.SLUG]
        approved = Amendment.objects.get(tenant=tenant, status=AmendmentStatus.APPROVED)
        result = StudentTermResult.objects.get(
            class_term=approved.class_term, student=approved.student,
        )
        self.assertEqual(result.version, 2)
        self.assertIn("cumulative", result.snapshot)
        self.assertEqual(len(result.snapshot["cumulative"]["terms"]), 3)

    def test_a_second_run_in_the_last_term_creates_nothing(self):
        second = self.run_seed()

        self.assertIn("Created this run: 0", second)
        self.assertNotIn("  + ", second)
        self.assertEqual(
            _counts(self.tenants[self.SLUG]), self.after_first_run[self.SLUG],
        )
        self.assertEqual(mail.outbox, [])


@tag("slow")
class FamiliesWhoOweTests(SeededSchools, TestCase):
    """Lagoon View's books bill three families for the term, so fee holds have balances to show.

    The first run finds no books and says so. The school's books are then opened
    and given the finance module's starter fee structure, as the dev database has
    them, and the second run bills the families through the Finance layer.
    """

    SLUGS = ("lagoon-view",)
    ONLY = "lagoon-view"
    SLUG = "lagoon-view"

    @classmethod
    def setUpTestData(cls):
        from schools.core.fal.adapters.django_finance import DjangoEntityResolverAdapter

        super().setUpTestData()
        school = School.objects.get(slug=cls.SLUG)
        with school_clock(cls.AT):
            DjangoEntityResolverAdapter().provision_entity(
                school.pk, code="LAGOONVIEW", name=school.name,
            ).unwrap()
            _build("seed_finance_ar_demo", entity="LAGOONVIEW")
        cls.billed = cls.run_seed()
        cls.after_billing = _counts(cls.tenants[cls.SLUG])

    def holds(self, **query):
        bursar = self.person(self.SLUG, "Ngozi", "Ibekwe")
        url = reverse("gradebook-holds") + "?" + urlencode({"term": self.term(self.SLUG).pk, **query})
        with school_clock(self.AT):
            response = TenantAPIClient(bursar).get(url)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_a_school_with_no_fee_structure_bills_nobody_and_says_how_to_add_one(self):
        self.assertIn("- no family owes:", self.first_run)
        self.assertIn("seed_finance_ar_demo --entity", self.first_run)

    def test_three_families_are_billed_and_owe_the_whole_bill(self):
        self.assertIn("+ 3 families billed FS-TERM1 for", self.billed)
        body = self.holds(owes="true")
        self.assertTrue(body["owes_applied"])
        owing = [row for row in body["data"] if row["balance"]]
        self.assertEqual(len(owing), 3)
        for row in owing:
            self.assertGreater(row["balance"], 0)
            self.assertIsNotNone(row["oldest_unpaid_on"])
            self.assertIsNone(row["hold"])

    def test_one_family_owes_with_a_result_already_released_and_two_can_still_be_held(self):
        owing = [row for row in self.holds(owes="true")["data"] if row["balance"]]
        self.assertEqual(sorted(row["released"] for row in owing), [False, False, True])
        self.assertEqual({row["class"]["name"] for row in owing if not row["released"]}, {"SSS1 A"})
        self.assertEqual({row["class"]["name"] for row in owing if row["released"]}, {"JSS1 A"})

    def test_a_further_run_bills_nobody(self):
        again = self.run_seed()
        self.assertIn("Created this run: 0", again)
        self.assertNotIn("families billed", again)
        self.assertEqual(_counts(self.tenants[self.SLUG]), self.after_billing)


@tag("slow")
class GradebookHistoryTests(SeededSchools, TestCase):
    """``seed_gradebook_history`` replays last year for the pupils now in JSS2 A, and leaves the year closed."""

    SLUGS = ("lagoon-view",)
    ONLY = "lagoon-view"
    SLUG = "lagoon-view"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        tenant = cls.tenants[cls.SLUG]
        cls.year = (
            AcademicSession.all_objects.filter(tenant=tenant, status="ARCHIVED")
            .order_by("-start_date").first()
        )
        cls.archived_at = cls.year.archived_at
        cls.notified = Notification.objects.filter(tenant=tenant).count()
        cls.history = cls.run_history()
        cls.after_history = _counts(tenant)

    @classmethod
    def run_history(cls, **options) -> str:
        out = StringIO()
        with school_clock(cls.AT):
            call_command("seed_gradebook_history", stdout=out, **options)
        return out.getvalue()

    def last_class(self):
        return SchoolClass.all_objects.get(
            tenant=self.tenants[self.SLUG], session=self.year, name="JSS1 A", is_active=True,
        )

    def test_the_year_is_archived_again_at_the_same_moment(self):
        self.year.refresh_from_db()
        self.assertEqual(self.year.status, "ARCHIVED")
        self.assertEqual(self.year.archived_at, self.archived_at)
        self.assertIn("is archived again", self.history)

    def test_the_pupils_now_in_jss2_a_were_in_last_years_jss1_a_and_promoted_out(self):
        from schools.vs_students.models import ClassEnrolment

        now = self.klass(self.SLUG, "JSS2 A")
        with school_clock(self.AT):
            rolls = book.rolls_for(self.tenants[self.SLUG], self.term(self.SLUG), [(now, now.branch)])
        roll = {entry.student.pk for entry in rolls[now.pk]}
        placed = ClassEnrolment.objects.filter(school_class=self.last_class())
        self.assertEqual({row.student_id for row in placed}, roll)
        self.assertEqual({row.outcome for row in placed}, {"PROMOTED"})
        self.assertFalse(any(row.is_active for row in placed))

    def test_every_term_of_last_year_is_published_and_the_third_carries_the_year(self):
        terms = ClassTerm.objects.filter(school_class=self.last_class()).order_by("term__order_index")
        self.assertEqual([t.stage for t in terms], [StoredStage.PUBLISHED] * 3)
        for each in terms:
            self.assertEqual(each.published_at.date(), each.term.end_date, "dated at its own term's end")
        result = StudentTermResult.objects.filter(class_term=terms.last()).first()
        block = result.snapshot["cumulative"]
        self.assertEqual(len(block["terms"]), 3)
        totals = {tuple(t["total"] for t in subject["terms"]) for subject in block["subjects"]}
        self.assertTrue(any(len(set(row)) > 1 for row in totals), "the terms should differ")

    def test_nobody_is_told_about_a_year_that_has_finished(self):
        self.assertEqual(Notification.objects.filter(tenant=self.tenants[self.SLUG]).count(), self.notified)

    def test_a_second_run_creates_nothing(self):
        again = self.run_history()
        self.assertIn("Created this run: 0", again)
        self.assertEqual(_counts(self.tenants[self.SLUG]), self.after_history)

    def test_the_year_is_closed_again_when_a_step_fails(self):
        from unittest import mock

        from schools.vs_gradebook.management.commands import seed_gradebook_history as history

        with mock.patch.object(history.Command, "_place", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                self.run_history()
        self.year.refresh_from_db()
        self.assertEqual((self.year.status, self.year.archived_at), ("ARCHIVED", self.archived_at))
