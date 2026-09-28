"""A school's promotion rules: suspended pupils, unplaced pupils, arms, capacity.

Security first: who may read and change the rules, the reach a writer needs,
and that one school's rules never reach another. Then each rule's effect on
the preview and the run, which share one classification, so every behaviour
test checks the run moves exactly the pupils the preview named.

Brightfield runs Lekki and Ikeja. This year's JSS1 A is school-wide, JSS1 B is
Lekki's and JSS1 C is Ikeja's. Next year has its own JSS1 A and a JSS2 per
test, built the way a roll-forward builds them: same codes, new rows.
"""
from __future__ import annotations

from django.urls import reverse

from schools.vs_academics.models import Level, SchoolClass
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)

from ..constants import EnrolmentOutcome, PromotionOutcome, StudentStatus
from ..models import ClassEnrolment
from ..services.promotion_rules import read_promotion_rules
from .base import StudentsFixture

DEFAULTS = {
    "suspended": "HOLD", "not_placed": "HOLD", "arms": "SAME_ARM",
    "capacity_mode": "FOLLOW_ENROLMENT",
}


class _PromotionRulesFixture(StudentsFixture):
    """A settings administrator for the whole school, one pinned to Lekki, and next year."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        settings_key = make_permission(
            "school.settings.update", scope=PermissionScope.TENANT,
        )
        role = make_role(cls.school, name="Settings Admin", key="settings_admin")
        make_role_permission(role, settings_key)
        make_role_permission(role, cls.permissions["school.students.view"])

        cls.settings_admin = make_school_admin(
            None, email="settings@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.settings_admin, role, branch=None)

        cls.kemi = make_school_admin(
            cls.lekki, email="kemi@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.kemi, role, branch=cls.lekki)

        cls.next_jss2 = Level.all_objects.create(
            tenant=cls.tenant, program=cls.program, session=cls.next_year,
            name="JSS2", code="JSS2", order_index=2,
        )
        cls.next_jss1 = Level.all_objects.create(
            tenant=cls.tenant, program=cls.program, session=cls.next_year,
            name="JSS1", code="JSS1", order_index=1, next_level=cls.next_jss2,
        )
        cls.next_jss1_a = SchoolClass.all_objects.create(
            tenant=cls.tenant, level=cls.next_jss1, session=cls.next_year,
            name="JSS1 A", code="N-JSS1A", arm="A", capacity=None,
        )

    def next_class(self, arm, *, branch=None, capacity=None):
        return SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.next_jss2, session=self.next_year,
            name=f"JSS2 {arm}", code=f"N-JSS2{arm}{branch.pk if branch else ''}",
            arm=arm, capacity=capacity, branch=branch,
        )

    def set_rules(self, **overrides):
        response = self.put(
            self.settings_admin, "student-promotion-rules",
            {**DEFAULTS, **overrides},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def set_enrolment_capacity(self, mode):
        from ..services.rules import read_rules, write_rules

        current = read_rules(self.tenant)
        write_rules(
            self.tenant, self.settings_admin,
            min_age_years=current.min_age_years,
            max_age_years=current.max_age_years,
            required_documents=list(current.required_documents),
            required_fields=list(current.required_fields),
            capacity_mode=mode, default_capacity=current.default_capacity,
        )

    def _url(self, name, branch=None):
        url = f"{reverse(name)}?tenant={self.tenant.slug}"
        return f"{url}&branch={branch.pk}" if branch is not None else url

    def preview(self, branch=None, **body):
        response = self.client_for(self.admin).post(
            self._url("student-promotion-preview", branch),
            {"to_session": self.next_year.pk, **body}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def run_promotion(self, branch=None, **body):
        return self.client_for(self.admin).post(
            self._url("student-promotion-run", branch),
            {"to_session": self.next_year.pk, **body}, format="json",
        )

    def placement(self, student):
        row = ClassEnrolment.all_objects.filter(
            student=student, session=self.next_year, is_active=True,
        ).first()
        return row.school_class.name if row else None

    @staticmethod
    def rows(data):
        return {row["id"]: row for row in data["students"]}


# ── security ────────────────────────────────────────────────────────────────

class PromotionRulesSecurityTests(_PromotionRulesFixture):

    def test_reading_the_rules_needs_the_students_view_key(self):
        self.assertEqual(
            self.get(self.nobody, "student-promotion-rules").status_code, 403,
        )
        self.assertEqual(
            self.get(self.admin, "student-promotion-rules").status_code, 200,
        )

    def test_changing_the_rules_needs_the_settings_key_not_a_student_key(self):
        """Adaeze holds every student key, promote included, and still may not."""
        body = {**DEFAULTS, "arms": "SPREAD"}
        self.assertEqual(
            self.put(self.admin, "student-promotion-rules", body).status_code, 403,
        )
        self.assertEqual(
            self.put(self.nobody, "student-promotion-rules", body).status_code, 403,
        )
        self.assertEqual(read_promotion_rules(self.tenant).arms, "SAME_ARM")

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        """Kemi spreading Lekki's arms would spread Ikeja's too."""
        response = self.put(
            self.kemi, "student-promotion-rules",
            {**DEFAULTS, "suspended": "PROMOTE", "arms": "SPREAD"},
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "SHARED_RECORD_READ_ONLY")
        self.assertEqual(
            response.data["message"],
            "Only a school-wide administrator can change the school's promotion rules.",
        )
        self.assertEqual(read_promotion_rules(self.tenant).stored(), DEFAULTS)

    def test_a_school_wide_caller_changes_them(self):
        self.set_rules(arms="SPREAD")
        self.assertEqual(read_promotion_rules(self.tenant).arms, "SPREAD")

    def test_one_schools_rules_never_reach_another(self):
        self.set_rules(
            suspended="PROMOTE", not_placed="PROMOTE", arms="SPREAD",
            capacity_mode="OFF",
        )
        theirs = self.get(self.solo_admin, "student-promotion-rules").data["data"]
        self.assertEqual(
            {key: theirs[key] for key in DEFAULTS}, DEFAULTS,
        )

    def test_the_request_cannot_name_another_school(self):
        response = self.put(
            self.settings_admin, "student-promotion-rules",
            {**DEFAULTS, "arms": "SPREAD", "tenant": self.solo.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(read_promotion_rules(self.solo.tenant).arms, "SAME_ARM")


# ── the rules endpoint ──────────────────────────────────────────────────────

class PromotionRulesShapeTests(_PromotionRulesFixture):

    def test_a_school_that_has_set_nothing_reads_todays_behaviour(self):
        data = self.get(self.admin, "student-promotion-rules").data["data"]
        self.assertEqual({key: data[key] for key in DEFAULTS}, DEFAULTS)
        self.assertEqual(data["effective_capacity_mode"], "WARN")
        self.assertEqual(data["enrolment_capacity_mode"], "WARN")
        self.assertEqual(data["options"], {
            "suspended": [
                {"value": "HOLD", "label": "Hold them where they are"},
                {"value": "PROMOTE", "label": "Move them up, still suspended"},
            ],
            "not_placed": [
                {"value": "HOLD", "label": "Hold them where they are"},
                {"value": "PROMOTE", "label": "Move them up with their class"},
            ],
            "arms": [
                {"value": "SAME_ARM", "label": "Keep each arm together"},
                {"value": "SPREAD", "label": "Share pupils evenly across the classes"},
            ],
            "capacity_mode": [
                {"value": "FOLLOW_ENROLMENT", "label": "Same as the enrolment rule"},
                {"value": "WARN", "label": "Warn, and let staff go ahead"},
                {"value": "HARD", "label": "Refuse, with no override"},
                {"value": "OFF", "label": "Do not check"},
            ],
        })

    def test_a_save_round_trips_and_answers_with_the_rules(self):
        body = {
            "suspended": "PROMOTE", "not_placed": "PROMOTE", "arms": "SPREAD",
            "capacity_mode": "HARD", "reason": "Agreed at the board meeting.",
        }
        response = self.put(self.settings_admin, "student-promotion-rules", body)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["message"], "Promotion rules saved.")
        saved = response.data["data"]
        self.assertEqual(saved["arms"], "SPREAD")
        self.assertEqual(saved["effective_capacity_mode"], "HARD")
        self.assertEqual(saved["enrolment_capacity_mode"], "WARN")
        again = self.get(self.admin, "student-promotion-rules").data["data"]
        self.assertEqual(again, saved)

    def test_following_the_enrolment_rule_reads_it_live(self):
        self.set_enrolment_capacity("HARD")
        data = self.get(self.admin, "student-promotion-rules").data["data"]
        self.assertEqual(data["capacity_mode"], "FOLLOW_ENROLMENT")
        self.assertEqual(data["effective_capacity_mode"], "HARD")
        self.assertEqual(data["enrolment_capacity_mode"], "HARD")

    def test_every_write_is_audited_with_its_reason_and_an_unchanged_save_writes_nothing(self):
        from vs_config.models import ConfigurationAuditEvent

        before = ConfigurationAuditEvent.objects.count()
        self.set_rules(arms="SPREAD", capacity_mode="OFF", reason="Three new classrooms.")
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before + 2)
        latest = ConfigurationAuditEvent.objects.order_by("-id").first()
        self.assertEqual(latest.reason, "Three new classrooms.")
        self.set_rules(arms="SPREAD", capacity_mode="OFF")
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before + 2)

    def test_each_refusal_is_a_sentence_keyed_on_its_field_and_nothing_is_written(self):
        response = self.put(self.settings_admin, "student-promotion-rules", {
            "suspended": "EXPEL", "not_placed": "MAYBE", "arms": "SHUFFLE",
            "capacity_mode": "SOMETIMES",
        })
        self.assertEqual(response.status_code, 400, response.data)
        detail = response.data["error"]["detail"]
        self.assertEqual(detail["suspended"], [
            "Choose HOLD or PROMOTE for what happens to suspended pupils at "
            "promotion.",
        ])
        self.assertEqual(detail["not_placed"], [
            "Choose HOLD or PROMOTE for what happens to pupils who are "
            "confirmed but not placed.",
        ])
        self.assertEqual(detail["arms"], [
            "Choose SAME_ARM or SPREAD for how promoted pupils are placed in "
            "next year's classes.",
        ])
        self.assertEqual(detail["capacity_mode"], [
            "Choose FOLLOW_ENROLMENT, WARN, HARD or OFF for what the promotion "
            "does when a class is full.",
        ])
        self.assertEqual(read_promotion_rules(self.tenant).stored(), DEFAULTS)

    def test_every_rule_is_sent_every_time(self):
        response = self.put(
            self.settings_admin, "student-promotion-rules", {"arms": "SPREAD"},
        )
        self.assertEqual(response.status_code, 400, response.data)
        detail = response.data["error"]["detail"]
        self.assertEqual(
            detail["suspended"],
            ["Say what happens to suspended pupils at promotion."],
        )
        self.assertEqual(
            detail["capacity_mode"],
            ["Say what the promotion does when a class is full."],
        )
        self.assertNotIn("arms", detail)
        self.assertEqual(read_promotion_rules(self.tenant).arms, "SAME_ARM")


# ── what the rules do ──────────────────────────────────────────────────────

class SuspendedAtPromotionTests(_PromotionRulesFixture):
    """Kelechi is suspended in JSS1 A; Chiamaka, beside him, is not."""

    def setUp(self):
        self.next_a = self.next_class("A")
        self.chiamaka = self.student(first="Chiamaka", last="Nwosu")
        self.place(self.chiamaka, self.shared_class)
        self.kelechi = self.student(
            first="Kelechi", last="Eze", status=StudentStatus.SUSPENDED,
        )
        self.place(self.kelechi, self.shared_class)

    def test_hold_names_him_as_an_exception_and_leaves_him_where_he_is(self):
        data = self.preview()
        self.assertEqual(data["rules"], DEFAULTS)
        self.assertNotIn(self.kelechi.pk, self.rows(data))
        entry = next(
            e for e in data["exceptions"]["by_student"]
            if e["student"] == self.kelechi.pk
        )
        self.assertEqual(entry["cause"], "STUDENT_SUSPENDED")
        self.assertEqual(
            entry["reason"],
            "Kelechi is suspended, so they are not promoted with the cohort. "
            "Lift the suspension first, or move them by hand afterwards.",
        )
        self.assertFalse(self.rows(data)[self.chiamaka.pk]["suspended"])

        response = self.run_promotion()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(self.placement(self.kelechi))
        self.assertEqual(self.placement(self.chiamaka), "JSS2 A")

    def test_promote_moves_him_up_still_suspended(self):
        self.set_rules(suspended="PROMOTE")
        data = self.preview()
        self.assertEqual(data["rules"]["suspended"], "PROMOTE")
        row = self.rows(data)[self.kelechi.pk]
        self.assertTrue(row["suspended"])
        self.assertEqual(row["outcome"], PromotionOutcome.PROMOTE)
        self.assertEqual(row["to_class"], "JSS2 A")
        self.assertEqual(data["exceptions"]["by_student"], [])

        response = self.run_promotion()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["promoted"], 2)
        self.assertEqual(self.placement(self.kelechi), "JSS2 A")
        self.kelechi.refresh_from_db()
        self.assertEqual(self.kelechi.status, StudentStatus.SUSPENDED)
        old = ClassEnrolment.all_objects.get(student=self.kelechi, session=self.year)
        self.assertEqual(old.outcome, EnrolmentOutcome.PROMOTED)
        self.assertFalse(old.is_active)

    def test_under_promote_the_review_screen_can_still_hold_or_repeat_him(self):
        self.set_rules(suspended="PROMOTE")
        held = self.preview(overrides={str(self.kelechi.pk): "HOLD"})
        self.assertEqual(self.rows(held)[self.kelechi.pk]["outcome"], "HOLD")

        response = self.run_promotion(overrides={str(self.kelechi.pk): "REPEAT"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.placement(self.kelechi), "JSS1 A")
        self.kelechi.refresh_from_db()
        self.assertEqual(self.kelechi.status, StudentStatus.SUSPENDED)


class NotPlacedAtPromotionTests(_PromotionRulesFixture):
    """Emeka is confirmed but not placed, and still holds JSS1 A."""

    def setUp(self):
        self.next_class("A")
        self.emeka = self.student(
            first="Emeka", last="Okafor", status=StudentStatus.ENROLLED,
        )
        self.place(self.emeka, self.shared_class)

    def test_hold_leaves_him_where_he_is(self):
        row = self.rows(self.preview())[self.emeka.pk]
        self.assertEqual(row["outcome"], PromotionOutcome.HOLD)
        response = self.run_promotion()
        self.assertEqual(response.data["data"]["held"], 1)
        self.assertIsNone(self.placement(self.emeka))

    def test_promote_moves_him_up_with_his_class_and_keeps_his_status(self):
        self.set_rules(not_placed="PROMOTE")
        row = self.rows(self.preview())[self.emeka.pk]
        self.assertEqual(row["outcome"], PromotionOutcome.PROMOTE)
        response = self.run_promotion()
        self.assertEqual(response.data["data"]["promoted"], 1)
        self.assertEqual(self.placement(self.emeka), "JSS2 A")
        self.emeka.refresh_from_db()
        self.assertEqual(self.emeka.status, StudentStatus.ENROLLED)


class SpreadAtPromotionTests(_PromotionRulesFixture):
    """Seven pupils from JSS1 A and JSS1 B move into next year's JSS2 A, B and C.

    JSS2 A already holds two pupils placed there by hand. Spread by last name,
    emptiest class first, ties by class name: B and C fill first, then all
    three take turns, and each ends with three.
    """

    SPREAD_TO = {
        "Adebayo": "JSS2 B", "Bello": "JSS2 C", "Chukwu": "JSS2 B",
        "Danjuma": "JSS2 C", "Eze": "JSS2 A", "Falana": "JSS2 B",
        "Garba": "JSS2 C",
    }

    def setUp(self):
        self.next_a = self.next_class("A")
        self.next_b = self.next_class("B")
        self.next_c = self.next_class("C")
        for first in ("Ngozi", "Musa"):
            early = self.student(first=first, last="Early")
            self.place(early, self.next_a, session=self.next_year)

        self.pupils = {}
        sources = {
            "Garba": self.shared_class, "Adebayo": self.lekki_class,
            "Eze": self.shared_class, "Bello": self.shared_class,
            "Falana": self.lekki_class, "Chukwu": self.lekki_class,
            "Danjuma": self.shared_class,
        }
        for last, source in sources.items():
            pupil = self.student(first="Pupil", last=last)
            self.place(pupil, source)
            self.pupils[last] = pupil

    def test_the_pupils_are_shared_out_evenly_and_the_run_matches_the_preview(self):
        self.set_rules(arms="SPREAD")
        data = self.preview()
        self.assertEqual(data["rules"]["arms"], "SPREAD")
        rows = self.rows(data)
        self.assertEqual(
            {last: rows[p.pk]["to_class"] for last, p in self.pupils.items()},
            self.SPREAD_TO,
        )
        self.assertEqual(self.preview(), data)

        response = self.run_promotion()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["promoted"], 7)
        self.assertEqual(
            {last: self.placement(p) for last, p in self.pupils.items()},
            self.SPREAD_TO,
        )
        for target in (self.next_a, self.next_b, self.next_c):
            self.assertEqual(
                ClassEnrolment.all_objects.filter(
                    school_class=target, is_active=True,
                ).count(),
                3, target.name,
            )

    def test_the_level_map_lists_each_class_a_source_sends_pupils_to(self):
        self.set_rules(arms="SPREAD")
        by_source = {row["from"]: row for row in self.preview()["level_map"]}
        jss1_a = by_source["JSS1 A"]
        self.assertEqual(jss1_a["to_classes"], [
            {"id": self.next_a.pk, "name": "JSS2 A", "students": 1},
            {"id": self.next_c.pk, "name": "JSS2 C", "students": 3},
        ])
        self.assertEqual(jss1_a["to"], "JSS2 A, JSS2 C")
        self.assertIsNone(jss1_a["to_id"])
        self.assertEqual(jss1_a["students"], 4)
        jss1_b = by_source["JSS1 B"]
        self.assertEqual(jss1_b["to_classes"], [
            {"id": self.next_b.pk, "name": "JSS2 B", "students": 3},
        ])
        self.assertEqual(jss1_b["to"], "JSS2 B")
        self.assertEqual(jss1_b["to_id"], self.next_b.pk)

    def test_keeping_arms_gives_one_class_per_source(self):
        by_source = {row["from"]: row for row in self.preview()["level_map"]}
        self.assertEqual(by_source["JSS1 A"]["to_classes"], [
            {"id": self.next_a.pk, "name": "JSS2 A", "students": 4},
        ])
        self.assertEqual(by_source["JSS1 A"]["to_id"], self.next_a.pk)
        self.assertEqual(by_source["JSS1 B"]["to"], "JSS2 B")

    def test_a_repeat_keeps_its_arm(self):
        self.set_rules(arms="SPREAD")
        eze = self.pupils["Eze"]
        response = self.run_promotion(overrides={str(eze.pk): "REPEAT"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.placement(eze), "JSS1 A")

    def test_a_class_with_no_promoting_pupils_has_an_empty_list(self):
        self.set_rules(arms="SPREAD")
        overrides = {
            str(p.pk): "HOLD"
            for p in self.pupils.values()
            if ClassEnrolment.all_objects.get(
                student=p, session=self.year,
            ).school_class_id == self.lekki_class.pk
        }
        by_source = {
            row["from"]: row
            for row in self.preview(overrides=overrides)["level_map"]
        }
        self.assertEqual(by_source["JSS1 B"]["to_classes"], [])
        self.assertEqual(by_source["JSS1 B"]["to"], "JSS2 B")


class SpreadWithinABranchTests(_PromotionRulesFixture):
    """Next year's JSS2 A is school-wide, JSS2 B is Lekki's, JSS2 C is Ikeja's."""

    def setUp(self):
        self.next_a = self.next_class("A")
        self.next_b = self.next_class("B", branch=self.lekki)
        self.next_c = self.next_class("C", branch=self.ikeja)
        self.lekki_pupils = []
        for last in ("Adeleke", "Bakare", "Coker", "Dike"):
            pupil = self.student(first="Lekki", last=last, branch=self.lekki)
            self.place(pupil, self.shared_class)
            self.lekki_pupils.append(pupil)
        self.ikeja_pupil = self.student(first="Ikeja", last="Adamu", branch=self.ikeja)
        self.place(self.ikeja_pupil, self.ikeja_class)
        self.set_rules(arms="SPREAD")

    def test_a_branch_run_spreads_over_that_branchs_classes_and_the_school_wide_ones(self):
        data = self.preview(branch=self.lekki)
        rows = self.rows(data)
        self.assertNotIn(self.ikeja_pupil.pk, rows)
        self.assertEqual(
            [rows[p.pk]["to_class"] for p in self.lekki_pupils],
            ["JSS2 A", "JSS2 B", "JSS2 A", "JSS2 B"],
        )
        response = self.run_promotion(branch=self.lekki)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            [self.placement(p) for p in self.lekki_pupils],
            ["JSS2 A", "JSS2 B", "JSS2 A", "JSS2 B"],
        )
        self.assertFalse(
            ClassEnrolment.all_objects.filter(school_class=self.next_c).exists(),
        )

    def test_a_whole_school_run_keeps_each_pupil_at_their_own_branch(self):
        rows = self.rows(self.preview())
        for pupil in self.lekki_pupils:
            self.assertIn(rows[pupil.pk]["to_class"], ("JSS2 A", "JSS2 B"))
        self.assertIn(rows[self.ikeja_pupil.pk]["to_class"], ("JSS2 A", "JSS2 C"))


class PromotionCapacityRuleTests(_PromotionRulesFixture):
    """Next year's JSS2 A holds one seat, and two pupils are promoted into it."""

    def setUp(self):
        self.next_a = self.next_class("A", capacity=1)
        for first in ("Chiamaka", "Tunde"):
            self.place(self.student(first=first, last="Mover"), self.shared_class)

    def test_following_the_enrolment_rule_warns_where_enrolment_warns(self):
        data = self.preview()
        self.assertEqual(data["capacity_mode"], "WARN")
        self.assertEqual(data["rules"]["capacity_mode"], "FOLLOW_ENROLMENT")
        refused = self.run_promotion()
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "PROMOTION_OVER_CAPACITY")
        self.assertEqual(self.run_promotion(allow_over_capacity=True).status_code, 201)

    def test_its_own_hard_refuses_where_enrolment_only_warns(self):
        self.set_rules(capacity_mode="HARD")
        data = self.preview()
        self.assertEqual(data["capacity_mode"], "HARD")
        self.assertEqual(data["rules"]["capacity_mode"], "HARD")
        refused = self.run_promotion(allow_over_capacity=True)
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "CLASS_FULL")
        self.assertEqual(
            refused.data["message"],
            "This promotion would put JSS2 A (2 of 1) over capacity, and this "
            "school does not put classes over capacity. Add a class or move "
            "students first.",
        )
        self.assertFalse(
            ClassEnrolment.all_objects.filter(session=self.next_year).exists(),
        )

    def test_its_own_off_runs_where_enrolment_is_hard(self):
        self.set_enrolment_capacity("HARD")
        self.set_rules(capacity_mode="OFF")
        data = self.preview()
        self.assertEqual(data["capacity_mode"], "OFF")
        self.assertEqual(data["over_capacity"], [])
        response = self.run_promotion()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["promoted"], 2)

    def test_following_a_hard_enrolment_rule_refuses(self):
        self.set_enrolment_capacity("HARD")
        refused = self.run_promotion(allow_over_capacity=True)
        self.assertEqual(refused.data["error"]["code"], "CLASS_FULL")

    def test_spreading_counts_capacity_after_the_pupils_are_shared_out(self):
        """With JSS2 B beside it, one pupil goes to each and nothing is over."""
        self.next_class("B", capacity=1)
        self.assertEqual(len(self.preview()["over_capacity"]), 1)
        self.set_rules(arms="SPREAD")
        self.assertEqual(self.preview()["over_capacity"], [])
        response = self.run_promotion()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["promoted"], 2)
