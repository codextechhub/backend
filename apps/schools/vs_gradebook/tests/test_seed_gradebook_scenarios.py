"""``seed_gradebook_scenarios``: every state is reached, and a second run changes nothing.

The command hangs results off the schools the sibling seeders build, so this
class builds them the same way (permissions, notification catalogue, onboarding,
academic structure, students), with the school clock set to the day the dev
database was first seeded, then runs the gradebook seeder on a Monday six weeks
later. Both shapes of school are covered: Sunrise has one branch and Lagoon View
has two.

Tagged ``slow`` because the sibling seeders and the permission seed cost
several seconds between them.
"""
from __future__ import annotations

import datetime as dt
from contextlib import redirect_stdout
from io import StringIO
from urllib.parse import urlencode

from django.apps import apps
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, tag
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import Department, SchoolClass, Subject
from schools.vs_gradebook.constants import (
    ClassStage,
    EmailState,
    ResultState,
    SetupStatus,
    SheetStatus,
    StoredStage,
)
from schools.vs_gradebook.models import (
    AssessmentStructure,
    ClassTerm,
    GradingScale,
    ResultHold,
    ScoreSheet,
    Score,
    StudentTermResult,
)
from schools.vs_gradebook.services.rules import read_gradebook_rules
from schools.vs_schools.dev.fixtures import build_school
from schools.vs_schools.models import School
from schools.vs_staff.models import StaffProfile
from schools.vs_students.models import Student
from vs_audit.models import AuditEvent
from vs_notifications.models import Notification
from vs_user.models import User

from .base import school_clock

SEEDED_ON = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
MONDAY = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)
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


@tag("slow")
class SeedGradebookScenariosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with school_clock(SEEDED_ON):
            _build("seed_all_permissions")
            _build("seed_notification_event_types")
            _build("seed_notification_templates")
            for slug, (name, branched) in SCHOOLS.items():
                build_school(
                    slug=slug, name=name, live=True, extra_branch=branched,
                    with_onboarding=False,
                )
                _build("seed_academic_scenarios", only=slug)
                _build("seed_student_scenarios", only=slug)
        cls.tenants = {slug: School.objects.get(slug=slug).tenant for slug in SLUGS}
        cls.first_run = cls.run_seed()
        cls.emails = list(mail.outbox)
        cls.after_first_run = {slug: _counts(t) for slug, t in cls.tenants.items()}

    @classmethod
    def run_seed(cls, at=MONDAY, **options) -> str:
        """Run the seeder on *at*, with the emails it triggers sent as if committed."""
        out = StringIO()
        with school_clock(at), cls.captureOnCommitCallbacks(execute=True):
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
        with school_clock(MONDAY):
            from schools.vs_gradebook.services import resolve
            from vs_config.clock import tenant_today

            tenant = self.tenants[slug]
            return resolve.current_term(tenant, tenant_today(tenant))

    def klass(self, slug, name):
        return SchoolClass.objects.get(tenant=self.tenants[slug], name=name, is_active=True)

    def subject(self, slug, name):
        return Subject.all_objects.get(tenant=self.tenants[slug], name=name)

    def get(self, user, route, query=None, **kwargs):
        url = reverse(route, kwargs=kwargs)
        if query:
            url += "?" + urlencode(query)
        with school_clock(MONDAY):
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

    def test_a_report_card_email_says_the_card_is_ready_and_nothing_of_the_result(self):
        ready = [m for m in self.emails if "report card" in (m.subject + m.body).lower()]
        self.assertTrue(ready)
        for message in ready:
            body = message.body.lower()
            for forbidden in ("position", "average", "grade", "%"):
                self.assertNotIn(forbidden, body, message.subject)
