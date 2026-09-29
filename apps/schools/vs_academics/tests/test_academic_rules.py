"""A school's academic structure settings: its word for a term, its term names, its arms.

Security first: who may read and change the settings, the reach a writer
needs, and that one school's settings never reach another. Then the defaults
each term structure implies, the round trip, every refusal, and the places the
settings take effect: the school's word in the sentences it is shown, and the
arms "generate arms" makes.

Brightfield runs Lekki and Ikeja. Adaeze holds the class and session keys for
the whole school; Bisi is the whole-school settings administrator; Kemi holds
the same settings key pinned to Lekki; Tolu teaches at Brightfield and holds no
key at all. Sunrise is another school.
"""
from __future__ import annotations

from vs_config.models import ConfigurationAuditEvent
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)
from schools.vs_academics.models import AcademicTerm, SchoolClass
from schools.vs_academics.services.academic_rules import read_academic_rules
from schools.vs_academics.services.words import term_word

from .test_class_subject_endpoints import _Base

THREE_TERMS = {
    "term_word": "TERM",
    "term_word_options": [
        {"value": "TERM", "label": "Term"},
        {"value": "SEMESTER", "label": "Semester"},
    ],
    "term_names": ["First Term", "Second Term", "Third Term"],
    "default_arms": ["A", "B", "C"],
}

SAVED = {
    "term_word": "SEMESTER",
    "term_names": ["Harmattan", "Rain", "Dry", "Long Vacation"],
    "default_arms": ["Red", "Blue"],
}


class _RulesFixture(_Base):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key in (
            "academics.session.view", "academics.session.create",
            "academics.session.update",
        ):
            make_role_permission(
                cls.role, make_permission(key, scope=PermissionScope.TENANT),
            )

        settings_role = make_role(cls.school, name="Settings Admin", key="settings_admin")
        make_role_permission(
            settings_role,
            make_permission("school.settings.update", scope=PermissionScope.TENANT),
        )
        cls.bisi = make_school_admin(
            None, email="bisi@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.bisi, settings_role, branch=None)
        cls.kemi = make_school_admin(
            cls.lekki, email="kemi@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.kemi, settings_role, branch=cls.lekki)
        cls.tolu = make_school_admin(
            None, email="tolu@brightfield.test", tenant=cls.tenant,
        )
        cls.sunrise_admin = make_school_admin(
            None, email="head@sunrise.test", tenant=cls.other.tenant,
        )

    def put(self, user, body):
        from django.urls import reverse

        return self.client_for(user).put(
            f"{reverse('academics-rules')}?tenant={self.tenant.slug}", body,
            format="json",
        )

    def save(self, **overrides):
        response = self.put(self.bisi, {**SAVED, **overrides})
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def on_two_semesters(self, school=None):
        from schools.vs_schools.models import School

        School.objects.filter(pk=(school or self.school).pk).update(
            term_structure="2_SEMESTERS",
        )


# ── security ────────────────────────────────────────────────────────────────

class AcademicRulesSecurityTests(_RulesFixture):

    def test_any_member_of_the_school_reads_them_with_no_key(self):
        """Tolu's register prints the school's word, so he needs no key to read it."""
        response = self.get(self.tolu, "academics-rules")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], THREE_TERMS)

    def test_a_member_of_another_school_cannot_read_this_schools(self):
        response = self.get(self.sunrise_admin, "academics-rules")
        self.assertEqual(response.status_code, 404, response.data)

    def test_changing_them_needs_the_settings_key_not_an_academics_key(self):
        """Adaeze holds every class and session key and still may not."""
        for user in (self.admin, self.tolu):
            response = self.put(user, SAVED)
            self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(read_academic_rules(self.tenant).as_dict(), THREE_TERMS)

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        """Kemi renaming Lekki's terms would rename Ikeja's too."""
        before = ConfigurationAuditEvent.objects.count()
        response = self.put(self.kemi, SAVED)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "SHARED_RECORD_READ_ONLY")
        self.assertEqual(
            response.data["message"],
            "Only a school-wide administrator can change the school's academic "
            "structure settings.",
        )
        self.assertEqual(read_academic_rules(self.tenant).as_dict(), THREE_TERMS)
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before)

    def test_a_school_wide_caller_changes_them(self):
        self.save()
        self.assertEqual(read_academic_rules(self.tenant).term_word, "SEMESTER")

    def test_one_schools_settings_never_reach_another(self):
        self.save()
        self.assertEqual(read_academic_rules(self.other.tenant).as_dict(), THREE_TERMS)

    def test_the_request_cannot_name_another_school(self):
        self.save(tenant=self.other.tenant.slug)
        self.assertEqual(read_academic_rules(self.other.tenant).term_word, "TERM")
        self.assertEqual(read_academic_rules(self.tenant).term_word, "SEMESTER")


# ── the settings endpoint ───────────────────────────────────────────────────

class AcademicRulesShapeTests(_RulesFixture):

    def test_a_three_term_school_that_has_set_nothing_reads_its_structure(self):
        data = self.get(self.admin, "academics-rules").data["data"]
        self.assertEqual(data, THREE_TERMS)

    def test_a_two_semester_school_that_has_set_nothing_reads_its_structure(self):
        self.on_two_semesters()
        data = self.get(self.admin, "academics-rules").data["data"]
        self.assertEqual(data["term_word"], "SEMESTER")
        self.assertEqual(data["term_names"], ["First Semester", "Second Semester"])
        self.assertEqual(data["default_arms"], ["A", "B", "C"])

    def test_a_save_round_trips_and_answers_with_the_settings(self):
        response = self.save(reason="Agreed at the board meeting.")
        self.assertEqual(response.data["message"], "Academic structure settings saved.")
        saved = response.data["data"]
        self.assertEqual(saved, {**THREE_TERMS, **SAVED})
        self.assertEqual(self.get(self.tolu, "academics-rules").data["data"], saved)

    def test_a_saved_value_outlives_a_change_of_structure(self):
        """The structure is only the starting point; a school's own names win."""
        self.save(term_word="TERM")
        self.on_two_semesters()
        self.assertEqual(
            list(read_academic_rules(self.tenant).term_names), SAVED["term_names"],
        )

    def test_a_default_saved_unchanged_keeps_following_the_structure(self):
        """Saving what the screen showed is not a choice, so nothing is pinned.

        A school still setting up saves the screen as offered, then corrects
        its structure to two semesters: it reads Semester and two semesters.
        """
        self.save(term_word="TERM", term_names=THREE_TERMS["term_names"])
        self.on_two_semesters()
        rules = read_academic_rules(self.tenant)
        self.assertEqual(rules.term_word, "SEMESTER")
        self.assertEqual(list(rules.term_names), ["First Semester", "Second Semester"])

    def test_names_are_saved_without_their_surrounding_spaces(self):
        self.save(term_names=["  Harmattan ", "Rain"], default_arms=[" Gold "])
        rules = read_academic_rules(self.tenant)
        self.assertEqual(list(rules.term_names), ["Harmattan", "Rain"])
        self.assertEqual(list(rules.default_arms), ["Gold"])

    def test_every_write_is_audited_with_its_reason_and_an_unchanged_save_writes_nothing(self):
        before = ConfigurationAuditEvent.objects.count()
        self.save(reason="New calendar.")
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before + 3)
        latest = ConfigurationAuditEvent.objects.order_by("-id").first()
        self.assertEqual(latest.reason, "New calendar.")
        self.save()
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before + 3)

    def test_saving_the_defaults_a_school_already_reads_writes_nothing(self):
        before = ConfigurationAuditEvent.objects.count()
        response = self.put(self.bisi, {
            key: THREE_TERMS[key] for key in ("term_word", "term_names", "default_arms")
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before)

    def test_changing_the_word_never_renames_a_term(self):
        year = self.year
        AcademicTerm.all_objects.create(
            tenant=self.tenant, session=year, name="First Term", order_index=1,
            start_date=year.start_date, end_date=year.start_date.replace(month=12),
        )
        self.save()
        self.assertEqual(
            list(AcademicTerm.all_objects.filter(session=year).values_list("name", flat=True)),
            ["First Term"],
        )


class AcademicRulesRefusalTests(_RulesFixture):
    """Each refusal is a sentence keyed on its field, and nothing is written."""

    def refused(self, **overrides):
        before = ConfigurationAuditEvent.objects.count()
        response = self.put(self.bisi, {**SAVED, **overrides})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before)
        self.assertEqual(read_academic_rules(self.tenant).as_dict(), THREE_TERMS)
        return response.data["error"]["detail"]

    def test_every_setting_is_sent_every_time(self):
        response = self.put(self.bisi, {"term_word": "SEMESTER"})
        self.assertEqual(response.status_code, 400, response.data)
        detail = response.data["error"]["detail"]
        self.assertEqual(
            detail["term_names"], ["List the names of the school's semesters."],
        )
        self.assertEqual(
            detail["default_arms"],
            ["List the arms a level's classes are generated with."],
        )
        response = self.put(self.bisi, {
            "term_names": SAVED["term_names"], "default_arms": ["A"],
        })
        self.assertEqual(
            response.data["error"]["detail"]["term_word"],
            ["Say whether the school says Term or Semester."],
        )

    def test_the_word_is_term_or_semester_only(self):
        detail = self.refused(term_word="QUARTER")
        self.assertEqual(detail["term_word"], [
            "Choose Term or Semester for what the school calls the parts of its year.",
        ])

    def test_a_year_has_one_to_six_terms_refused_in_the_word_being_saved(self):
        self.assertEqual(
            self.refused(term_names=[])["term_names"], ["Name at least one semester."],
        )
        self.assertEqual(
            self.refused(term_names="First Term")["term_names"],
            ["Name at least one semester."],
        )
        seven = [f"Part {n}" for n in range(1, 8)]
        self.assertEqual(
            self.refused(term_word="TERM", term_names=seven)["term_names"],
            ["A year can have at most 6 terms."],
        )

    def test_six_terms_are_allowed(self):
        self.save(term_names=[f"Part {n}" for n in range(1, 7)])
        self.assertEqual(len(read_academic_rules(self.tenant).term_names), 6)

    def test_a_term_name_is_never_blank(self):
        for blank in ("", "   ", None, 3):
            detail = self.refused(term_names=["Harmattan", blank])
            self.assertEqual(detail["term_names"], ["Every semester needs a name."])

    def test_a_term_name_is_at_most_thirty_characters(self):
        long = "The Very Long First Semester Name"
        self.assertEqual(
            self.refused(term_names=[long])["term_names"],
            [f"{long} is longer than 30 characters. Shorten it."],
        )
        self.save(term_names=["x" * 30])

    def test_two_terms_never_share_a_name_whatever_its_case(self):
        self.assertEqual(
            self.refused(term_names=["Harmattan", "HARMATTAN "])["term_names"],
            ["HARMATTAN is listed twice. Give each semester a different name."],
        )

    def test_the_default_arms_follow_the_same_rules_up_to_twelve(self):
        self.assertEqual(
            self.refused(default_arms=[])["default_arms"], ["Name at least one arm."],
        )
        self.assertEqual(
            self.refused(default_arms=[str(n) for n in range(13)])["default_arms"],
            ["The default list can have at most 12 arms."],
        )
        self.assertEqual(
            self.refused(default_arms=["A", " "])["default_arms"],
            ["Every arm needs a name."],
        )
        self.assertEqual(
            self.refused(default_arms=["a", "A"])["default_arms"],
            ["A is listed twice. Give each arm a different name."],
        )
        self.assertEqual(
            self.refused(default_arms=["y" * 31])["default_arms"],
            [f"{'y' * 31} is longer than 30 characters. Shorten it."],
        )
        self.save(default_arms=[str(n) for n in range(12)])


# ── where the settings take effect ─────────────────────────────────────────

class SchoolWordTests(_RulesFixture):
    """Sentences about a term say the school's word; a term's own name is untouched."""

    def create_year(self, terms):
        from django.urls import reverse

        return self.client_for(self.admin).post(
            f"{reverse('academics-session-list')}?tenant={self.tenant.slug}",
            {
                "name": "2100/2101", "start_date": "2100-09-01",
                "end_date": "2101-07-31", "terms": terms,
            },
            format="json",
        )

    def two_terms_named(self, first, second):
        return [
            {"name": first, "order_index": 1,
             "start_date": "2100-09-10", "end_date": "2100-12-15"},
            {"name": second, "order_index": 2,
             "start_date": "2101-01-10", "end_date": "2101-04-10"},
        ]

    def test_a_term_school_is_told_about_its_terms(self):
        response = self.create_year(self.two_terms_named("Harmattan", "harmattan"))
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["message"],
            "This year already has a term called harmattan. Give this one a "
            "different name.",
        )

    def test_a_semester_school_is_told_about_its_semesters(self):
        self.on_two_semesters()
        response = self.create_year(self.two_terms_named("Harmattan", "harmattan"))
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["message"],
            "This year already has a semester called harmattan. Give this one a "
            "different name.",
        )

    def test_the_school_choice_wins_over_its_structure(self):
        self.save(term_word="SEMESTER")
        terms = self.two_terms_named("First Term", "Second Term")
        terms[1]["order_index"] = 1
        response = self.create_year(terms)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["message"],
            "First Term is already semester 1 of this year. Give this one a "
            "different number.",
        )

    def test_listing_a_years_terms_says_the_schools_word(self):
        self.save()
        created = self.create_year(self.two_terms_named("First Term", "Second Term"))
        self.assertEqual(created.status_code, 201, created.data)
        response = self.get(
            self.admin, "academics-term-list", pk=created.data["data"]["id"],
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["message"], "Semesters retrieved.")
        self.assertEqual(
            [t["name"] for t in response.data["data"]], ["First Term", "Second Term"],
        )

    def test_the_helper_gives_every_form(self):
        self.save()
        self.assertEqual(term_word(self.tenant), "semester")
        self.assertEqual(term_word(self.tenant, plural=True), "semesters")
        self.assertEqual(term_word(self.tenant, capital=True), "Semester")
        self.assertEqual(term_word(self.other.tenant, plural=True, capital=True), "Terms")


class GenerateArmsDefaultTests(_RulesFixture):
    """A request naming no arms makes the school's default arms."""

    def generate(self, **body):
        return self.post(
            self.admin, "academics-class-arms",
            {"level": self.jss1.pk, "branch": None, **body},
        )

    def names(self):
        return list(
            SchoolClass.all_objects.filter(level=self.jss1)
            .order_by("name").values_list("name", flat=True),
        )

    def test_a_school_that_has_set_nothing_gets_a_b_and_c(self):
        response = self.generate()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.names(), ["JSS1 A", "JSS1 B", "JSS1 C"])

    def test_the_schools_own_arms_are_used_and_named_after_the_level(self):
        self.save(default_arms=["Gold", "Diamond"])
        response = self.generate()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.names(), ["JSS1 Diamond", "JSS1 Gold"])
        self.assertEqual(
            sorted(SchoolClass.all_objects.filter(level=self.jss1).values_list("arm", flat=True)),
            ["Diamond", "Gold"],
        )

    def test_arms_named_in_the_request_win(self):
        self.save(default_arms=["Gold", "Diamond"])
        response = self.generate(arms=["X"])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.names(), ["JSS1 X"])

    def test_another_schools_arms_are_not_this_schools(self):
        from schools.vs_academics.services.academic_rules import write_academic_rules

        write_academic_rules(
            self.other.tenant, self.sunrise_admin, term_word="TERM",
            term_names=["One"], default_arms=["Violet"],
        )
        response = self.generate()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.names(), ["JSS1 A", "JSS1 B", "JSS1 C"])
