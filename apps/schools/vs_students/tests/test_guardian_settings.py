"""A school's guardian rules: how many, whether an email, how matched, which relationships.

Security first: who may read and change the rules, and that one school's rules
never reach another. Then each rule's effect on every path it governs: the
enrolment form, saving an applicant, the link drawer, the guardian correction
form, unlinking, and both spreadsheet imports. Brightfield has two branches
and Sunrise one, so the rules are seen at both shapes of school.
"""
from __future__ import annotations

from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)

from ..constants import Relationship, StudentStatus
from ..models import Guardian, Student, StudentGuardian
from .base import StudentsFixture

EMAIL_REQUIRED = "A guardian email is required at this school."


class _GuardianRulesFixture(StudentsFixture):
    """The shared fixture plus a settings admin at each school."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        settings_key = make_permission(
            "school.settings.update", scope=PermissionScope.TENANT,
        )
        view_key = cls.permissions["school.students.view"]

        role = make_role(cls.school, name="Settings Admin", key="settings_admin")
        make_role_permission(role, settings_key)
        make_role_permission(role, view_key)
        cls.settings_admin = make_school_admin(
            None, email="settings@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.settings_admin, role, branch=None)

        solo_role = make_role(cls.solo, name="Settings Admin", key="settings_admin")
        make_role_permission(solo_role, settings_key)
        make_role_permission(solo_role, view_key)
        cls.solo_settings = make_school_admin(
            None, email="settings@sunrise.test", tenant=cls.solo.tenant,
        )
        make_assignment(cls.solo, cls.solo_settings, solo_role, branch=None)

    def rules_body(self, **overrides):
        body = {
            "min_per_student": 1, "email_required": False,
            "matching": "EMAIL_THEN_PHONE", "extra_relationships": [],
        }
        body.update(overrides)
        return body

    def set_rules(self, user=None, **overrides):
        response = self.put(
            user or self.settings_admin, "student-guardian-rules",
            self.rules_body(**overrides),
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def set_solo_rules(self, **overrides):
        return self.set_rules(user=self.solo_settings, **overrides)

    def guardian_row(self, **overrides):
        row = {
            "full_name": "Mrs. Amina Yusuf", "phone": "08115550177",
            "email": "amina.yusuf@example.ng",
            "relationship": Relationship.MOTHER, "is_primary": True,
        }
        row.update(overrides)
        return row

    def second_guardian(self, **overrides):
        return self.guardian_row(**{
            "full_name": "Mr. Bala Yusuf", "phone": "08115550178",
            "email": "bala.yusuf@example.ng",
            "relationship": Relationship.FATHER, "is_primary": False,
            **overrides,
        })

    def solo_class(self):
        """Sunrise's one class, made on first use."""
        from schools.vs_academics.models import Level, Program, SchoolClass

        if getattr(self, "_solo_class", None) is None:
            program = Program.all_objects.create(
                tenant=self.solo.tenant, name="Primary", code="PRY",
            )
            level = Level.all_objects.create(
                tenant=self.solo.tenant, program=program, session=self.solo_year,
                name="Primary 1", code="P1", order_index=1,
            )
            self._solo_class = SchoolClass.all_objects.create(
                tenant=self.solo.tenant, level=level, session=self.solo_year,
                name="Primary 1 A", code="P1A", arm="A", capacity=30, branch=None,
            )
        return self._solo_class

    def solo_enrolment_body(self, **overrides):
        """An enrolment at Sunrise, whose one branch needs no naming."""
        body = self.enrolment_body(school_class=self.solo_class().pk)
        body.pop("branch")
        body.update(overrides)
        return body

    def error_detail(self, response, status=400):
        self.assertEqual(response.status_code, status, response.data)
        return response.data["error"]["detail"]


# ── security ────────────────────────────────────────────────────────────────

class GuardianRulesSecurityTests(_GuardianRulesFixture):

    def test_reading_the_rules_needs_the_students_view_key(self):
        self.assertEqual(
            self.get(self.nobody, "student-guardian-rules").status_code, 403,
        )
        self.assertEqual(
            self.get(self.admin, "student-guardian-rules").status_code, 200,
        )

    def test_changing_the_rules_needs_the_settings_key_not_a_student_key(self):
        """Adaeze holds every student key and still may not change the rules."""
        refused = self.put(
            self.admin, "student-guardian-rules", self.rules_body(min_per_student=2),
        )
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(
            self.put(self.nobody, "student-guardian-rules", self.rules_body())
            .status_code, 403,
        )
        self.assertEqual(
            self.get(self.admin, "student-guardian-rules")
            .data["data"]["min_per_student"], 1,
        )
        self.set_rules(min_per_student=2)

    def test_one_schools_rules_never_reach_another(self):
        """Brightfield asks for everything; Sunrise has set nothing."""
        self.set_rules(
            min_per_student=2, email_required=True, matching="EMAIL_ONLY",
            extra_relationships=["Sponsor"],
        )
        theirs = self.get(self.solo_admin, "student-guardian-rules").data["data"]
        self.assertEqual(theirs["min_per_student"], 1)
        self.assertFalse(theirs["email_required"])
        self.assertEqual(theirs["matching"], "EMAIL_THEN_PHONE")
        self.assertEqual(theirs["extra_relationships"], [])

        # Sunrise still enrols with one guardian, no email, and refuses Sponsor.
        body = self.solo_enrolment_body(guardians=[
            self.guardian_row(email=""),
        ])
        allowed = self.post(self.solo_admin, "student-list", body)
        self.assertEqual(allowed.status_code, 201, allowed.data)
        sponsor = self.solo_enrolment_body(
            first_name="Musa",
            guardians=[self.guardian_row(relationship="Sponsor", phone="08115550999")],
        )
        detail = self.error_detail(self.post(self.solo_admin, "student-list", sponsor))
        self.assertIn("relationship", detail["guardians"][0])

    def test_a_single_branch_school_sets_its_own(self):
        self.set_solo_rules(min_per_student=3, extra_relationships=["Driver"])
        mine = self.get(self.solo_admin, "student-guardian-rules").data["data"]
        self.assertEqual(mine["min_per_student"], 3)
        self.assertEqual(mine["extra_relationships"], ["Driver"])
        self.assertEqual(
            self.get(self.admin, "student-guardian-rules")
            .data["data"]["min_per_student"], 1,
        )

    def test_the_request_cannot_name_another_school(self):
        """A tenant slug in the body is not a field, and the answer is the caller's school."""
        response = self.put(
            self.settings_admin, "student-guardian-rules",
            {**self.rules_body(min_per_student=4), "tenant": self.solo.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.get(self.solo_admin, "student-guardian-rules")
            .data["data"]["min_per_student"], 1,
        )


# ── the rules endpoint ──────────────────────────────────────────────────────

class GuardianRulesShapeTests(_GuardianRulesFixture):

    def test_a_school_that_has_set_nothing_reads_todays_behaviour(self):
        data = self.get(self.admin, "student-guardian-rules").data["data"]
        self.assertEqual(data, {
            "min_per_student": 1,
            "email_required": False,
            "matching": "EMAIL_THEN_PHONE",
            "matching_options": [
                {"value": "EMAIL_THEN_PHONE", "label": "Email, then phone"},
                {"value": "EMAIL_ONLY", "label": "Email only"},
            ],
            "extra_relationships": [],
            "relationships": [
                {"value": "MOTHER", "label": "Mother"},
                {"value": "FATHER", "label": "Father"},
                {"value": "UNCLE", "label": "Uncle"},
                {"value": "AUNT", "label": "Aunt"},
                {"value": "GRANDPARENT", "label": "Grandparent"},
                {"value": "LEGAL_GUARDIAN", "label": "Legal guardian"},
                {"value": "SIBLING", "label": "Sibling"},
                {"value": "OTHER", "label": "Other"},
            ],
        })

    def test_put_returns_the_refreshed_body_with_the_schools_relationships(self):
        data = self.set_rules(
            min_per_student=2, email_required=True, matching="EMAIL_ONLY",
            extra_relationships=["  Sponsor ", "Driver"],
            reason="Two contacts for every child.",
        ).data["data"]
        self.assertEqual(data["min_per_student"], 2)
        self.assertTrue(data["email_required"])
        self.assertEqual(data["matching"], "EMAIL_ONLY")
        # Trimmed, and in the order the school gave them.
        self.assertEqual(data["extra_relationships"], ["Sponsor", "Driver"])
        self.assertEqual(
            [r["value"] for r in data["relationships"]],
            ["MOTHER", "FATHER", "UNCLE", "AUNT", "GRANDPARENT",
             "LEGAL_GUARDIAN", "SIBLING", "Sponsor", "Driver", "OTHER"],
        )
        self.assertIn({"value": "Sponsor", "label": "Sponsor"}, data["relationships"])

    def test_every_write_is_audited_and_an_unchanged_save_writes_nothing(self):
        from vs_config.models import ConfigurationAuditEvent

        before = ConfigurationAuditEvent.objects.count()
        self.set_rules(min_per_student=2, extra_relationships=["Sponsor"])
        after_change = ConfigurationAuditEvent.objects.count()
        self.assertEqual(after_change - before, 2)
        self.set_rules(min_per_student=2, extra_relationships=["Sponsor"])
        self.assertEqual(ConfigurationAuditEvent.objects.count(), after_change)

    def assert_refused(self, field, **overrides):
        detail = self.error_detail(self.put(
            self.settings_admin, "student-guardian-rules",
            self.rules_body(**overrides),
        ))
        self.assertIn(field, detail, detail)
        return detail[field][0]

    def test_the_minimum_is_one_to_four(self):
        self.assertEqual(
            self.assert_refused("min_per_student", min_per_student=0),
            "Every child needs at least 1 guardian.",
        )
        self.assertEqual(
            self.assert_refused("min_per_student", min_per_student=5),
            "A school can ask for at most 4 guardians for every child.",
        )
        self.set_rules(min_per_student=4)

    def test_an_unknown_matching_mode_is_refused(self):
        self.assertEqual(
            self.assert_refused("matching", matching="PHONE_ONLY"),
            "Choose EMAIL_THEN_PHONE or EMAIL_ONLY for how guardians are matched.",
        )

    def test_the_schools_relationships_are_checked_one_by_one(self):
        self.assertEqual(
            self.assert_refused(
                "extra_relationships",
                extra_relationships=[f"Role {n}" for n in range(11)],
            ),
            "A school can add up to 10 relationships of its own.",
        )
        self.assertEqual(
            self.assert_refused("extra_relationships", extra_relationships=["  "]),
            "A relationship needs a name.",
        )
        self.assertEqual(
            self.assert_refused("extra_relationships", extra_relationships=["x" * 31]),
            f"'{'x' * 31}' is longer than 30 characters.",
        )
        self.assertEqual(
            self.assert_refused(
                "extra_relationships", extra_relationships=["Sponsor", "sponsor "],
            ),
            "'sponsor' is listed twice.",
        )
        for fixed in ("mother", "Legal Guardian", "legal_guardian", "OTHER"):
            with self.subTest(fixed=fixed):
                self.assertEqual(
                    self.assert_refused("extra_relationships", extra_relationships=[fixed]),
                    f"'{fixed}' is already a relationship every school has.",
                )
        self.set_rules(extra_relationships=["x" * 30])

    def test_every_rule_is_sent_every_time(self):
        body = self.rules_body()
        body.pop("matching")
        detail = self.error_detail(
            self.put(self.settings_admin, "student-guardian-rules", body),
        )
        self.assertEqual(detail["matching"], ["Say how guardians are matched."])


# ── the minimum number of guardians ─────────────────────────────────────────

class GuardianMinimumTests(_GuardianRulesFixture):
    """Brightfield and Sunrise both ask for two guardians for every child."""

    def setUp(self):
        self.set_rules(min_per_student=2)
        self.set_solo_rules(min_per_student=2)

    def test_enrolment_with_fewer_is_refused_on_guardians(self):
        detail = self.error_detail(
            self.post(self.admin, "student-list", self.enrolment_body()),
        )
        self.assertEqual(
            detail, {"guardians": ["This school asks for 2 guardians for every child."]},
        )
        self.assertFalse(Student.all_objects.filter(first_name="Zainab").exists())

        allowed = self.post(self.admin, "student-list", self.enrolment_body(
            guardians=[self.guardian_row(), self.second_guardian()],
        ))
        self.assertEqual(allowed.status_code, 201, allowed.data)

    def test_a_single_branch_school_holds_its_own_minimum(self):
        refused = self.post(self.solo_admin, "student-list", self.solo_enrolment_body())
        self.assertIn("guardians", self.error_detail(refused))

    def test_saving_an_applicant_with_fewer_is_refused_too(self):
        body = self.enrolment_body(
            as_applicant=True, applied_for=self.jss1.pk, school_class=None,
        )
        self.assertIn(
            "guardians",
            self.error_detail(self.post(self.admin, "student-list", body)),
        )
        body["guardians"] = [self.guardian_row(), self.second_guardian()]
        allowed = self.post(self.admin, "student-list", body)
        self.assertEqual(allowed.status_code, 201, allowed.data)

    def test_no_guardian_at_all_keeps_its_own_code_and_names_the_minimum(self):
        response = self.post(
            self.admin, "student-list", self.enrolment_body(guardians=[]),
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "GUARDIAN_REQUIRED")
        self.assertEqual(
            response.data["message"], "This school asks for 2 guardians for every child.",
        )

    def _family(self, *, status=StudentStatus.ACTIVE, count=2):
        child = self.student(status=status)
        guardians = []
        for n in range(count):
            guardian = self.guardian(
                name=f"Guardian {n}", phone=f"0803555020{n}",
                email=f"guardian{n}@example.ng",
            )
            self.link(child, guardian, primary=n == 0)
            guardians.append(guardian)
        return child, guardians

    def test_unlinking_below_the_minimum_is_refused_for_a_child_on_the_roll(self):
        child, (mother, father) = self._family()
        response = self.delete(
            self.admin, "student-guardian-detail", pk=child.pk, guardian_id=father.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "GUARDIAN_REQUIRED")
        self.assertEqual(
            response.data["message"],
            "This school asks for 2 guardians for every child, and Chiamaka has "
            "2. Link another before removing this one.",
        )
        self.assertEqual(child.guardian_links.count(), 2)

    def test_unlinking_down_to_the_minimum_is_allowed(self):
        child, guardians = self._family(count=3)
        response = self.delete(
            self.admin, "student-guardian-detail",
            pk=child.pk, guardian_id=guardians[2].pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_child_not_on_the_roll_can_lose_guardians(self):
        child, (mother, father) = self._family(status=StudentStatus.WITHDRAWN)
        response = self.delete(
            self.admin, "student-guardian-detail", pk=child.pk, guardian_id=father.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_school_with_the_default_can_unlink_down_to_one(self):
        """Sunrise goes back to one: removing a second guardian passes, the last does not."""
        self.set_solo_rules(min_per_student=1)
        child = self.student(tenant=self.solo.tenant, branch=self.solo_branch)
        mother = self.guardian(tenant=self.solo.tenant, name="Mrs. Ada Obi",
                               phone="08035550301", email="ada@example.ng")
        father = self.guardian(tenant=self.solo.tenant, name="Mr. Obi Obi",
                               phone="08035550302", email="obi@example.ng")
        self.link(child, mother, primary=True)
        self.link(child, father, primary=False)
        self.assertEqual(self.delete(
            self.solo_admin, "student-guardian-detail",
            pk=child.pk, guardian_id=father.pk,
        ).status_code, 200)
        last = self.delete(
            self.solo_admin, "student-guardian-detail",
            pk=child.pk, guardian_id=mother.pk,
        )
        self.assertEqual(last.status_code, 422)
        self.assertIn("only guardian", last.data["message"])

    def test_the_import_warns_on_every_row_and_still_imports(self):
        from .test_import_export import _ImportFixture

        rows = [
            _ImportFixture.row(self),
            _ImportFixture.row(self, **{
                "First Name": "Tobi", "Guardian Email": "bello@example.ng",
                "Guardian Phone": "08035550199", "Guardian Name": "Mrs. Ada Bello",
            }),
        ]
        issues = _import_issues(self, rows)
        self.assertFalse([i for i in issues if i["severity"] == "error"], issues)
        warnings = [
            i for i in issues
            if "asks for 2 guardians" in i["message"]
        ]
        self.assertEqual([w["row_number"] for w in warnings], [1, 2])
        self.assertTrue(all(w["severity"] == "warning" for w in warnings))

        student = _execute_import_row(self, rows[0])
        self.assertEqual(student.guardian_links.count(), 1)


# ── a guardian email ────────────────────────────────────────────────────────

class GuardianEmailRequiredTests(_GuardianRulesFixture):
    """Brightfield requires a guardian email; Mr Okafor was held from before."""

    def setUp(self):
        self.set_rules(email_required=True)
        self.okafor = self.guardian(
            name="Mr. Emeka Okafor", phone="08035550400", email="",
        )

    def test_enrolment_refuses_a_new_guardian_without_one(self):
        detail = self.error_detail(self.post(
            self.admin, "student-list",
            self.enrolment_body(guardians=[self.guardian_row(email="")]),
        ))
        self.assertEqual(detail, {"guardians": [{"email": [EMAIL_REQUIRED]}]})

    def test_enrolment_links_an_existing_guardian_without_one(self):
        by_id = self.post(self.admin, "student-list", self.enrolment_body(guardians=[
            {"guardian_id": self.okafor.pk, "relationship": "FATHER", "is_primary": True},
        ]))
        self.assertEqual(by_id.status_code, 201, by_id.data)

        # Named by the phone the school matches on, he is linked, not refused.
        by_phone = self.post(self.admin, "student-list", self.enrolment_body(
            first_name="Obinna",
            guardians=[self.guardian_row(
                full_name="Emeka Okafor", phone="08035550400", email="",
            )],
        ))
        self.assertEqual(by_phone.status_code, 201, by_phone.data)
        self.assertEqual(Guardian.all_objects.filter(phone="08035550400").count(), 1)

    def test_the_link_drawer_refuses_a_new_guardian_without_one(self):
        child = self.student()
        detail = self.error_detail(self.post(
            self.admin, "student-guardians",
            {"full_name": "Mrs. Nkechi Eze", "phone": "08035550500",
             "relationship": "AUNT"},
            pk=child.pk,
        ))
        self.assertEqual(detail, {"email": [EMAIL_REQUIRED]})
        allowed = self.post(
            self.admin, "student-guardians",
            {"guardian_id": self.okafor.pk, "relationship": "UNCLE"}, pk=child.pk,
        )
        self.assertEqual(allowed.status_code, 201, allowed.data)

    def test_creating_a_guardian_through_the_service_is_held_to_it(self):
        from rest_framework.exceptions import ValidationError

        from ..services.guardians import upsert_guardian

        with self.assertRaises(ValidationError) as caught:
            upsert_guardian(self.tenant, full_name="Mrs. Ada Nwosu", phone="08035550600")
        self.assertEqual(caught.exception.detail, {"email": [EMAIL_REQUIRED]})
        held, made = upsert_guardian(
            self.tenant, full_name="Emeka Okafor", phone="08035550400",
        )
        self.assertEqual((held, made), (self.okafor, False))

    def test_an_edit_may_not_blank_an_email(self):
        held = self.guardian(name="Mrs. Ada Nwosu", phone="08035550601",
                             email="ada.nwosu@example.ng")
        detail = self.error_detail(self.patch(
            self.admin, "guardian-detail", {"email": ""}, pk=held.pk,
        ))
        self.assertEqual(detail, {"email": [EMAIL_REQUIRED]})
        held.refresh_from_db()
        self.assertEqual(held.email, "ada.nwosu@example.ng")

    def test_an_edit_to_a_guardian_who_never_had_one_passes(self):
        response = self.patch(
            self.admin, "guardian-detail",
            {"phone": "08035550401", "email": ""}, pk=self.okafor.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_school_without_the_rule_may_blank_an_email(self):
        held = self.guardian(
            tenant=self.solo.tenant, name="Mrs. Ada Obi", phone="08035550602",
            email="ada.obi@example.ng",
        )
        response = self.patch(
            self.solo_admin, "guardian-detail", {"email": ""}, pk=held.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_student_import_refuses_a_blank_guardian_email(self):
        from .test_import_export import _ImportFixture

        issues = _import_issues(self, [_ImportFixture.row(self, **{"Guardian Email": ""})])
        errors = [i for i in issues if i["severity"] == "error"]
        self.assertEqual(
            [(e["column_name"], e["message"]) for e in errors],
            [("Guardian Email", EMAIL_REQUIRED)],
        )

    def test_the_guardian_import_refuses_a_new_guardian_and_links_a_held_one(self):
        child = self.student(number="BFS/1")
        new = _guardian_row(**{"Guardian Email": ""})
        held = _guardian_row(**{
            "Guardian Name": "Mr. Emeka Okafor", "Guardian Phone": "08035550400",
            "Guardian Email": "", "Relationship": "Uncle",
        })
        errors = [
            i for i in _guardian_import_issues(self, [new, held])
            if i["severity"] == "error"
        ]
        self.assertEqual(
            [(e["row_number"], e["message"]) for e in errors], [(1, EMAIL_REQUIRED)],
        )
        self.assertEqual(child.guardian_links.count(), 0)


# ── matching ────────────────────────────────────────────────────────────────

class GuardianMatchingTests(_GuardianRulesFixture):
    """The Bellos and the Okekes share the landline 01 234 5678 at Brightfield."""

    LANDLINE = "012345678"

    def enrol_two_families(self):
        first = self.post(self.admin, "student-list", self.enrolment_body(
            first_name="Tunde", last_name="Bello",
            guardians=[self.guardian_row(
                full_name="Mrs. Kemi Bello", phone=self.LANDLINE,
                email="kemi.bello@example.ng",
            )],
        ))
        self.assertEqual(first.status_code, 201, first.data)
        second = self.post(self.admin, "student-list", self.enrolment_body(
            first_name="Ifeoma", last_name="Okeke",
            guardians=[self.guardian_row(
                full_name="Mrs. Uche Okeke", phone=self.LANDLINE, email="",
            )],
        ))
        self.assertEqual(second.status_code, 201, second.data)
        return Guardian.all_objects.filter(tenant=self.tenant, phone=self.LANDLINE)

    def test_email_only_does_not_merge_two_families_on_a_landline(self):
        self.set_rules(matching="EMAIL_ONLY")
        held = self.enrol_two_families()
        self.assertEqual(
            sorted(g.full_name for g in held), ["Mrs. Kemi Bello", "Mrs. Uche Okeke"],
        )

    def test_email_then_phone_still_merges_them(self):
        """The behaviour every school had, and Brightfield's until it chooses."""
        held = self.enrol_two_families()
        self.assertEqual([g.full_name for g in held], ["Mrs. Kemi Bello"])

    def test_match_existing_follows_the_schools_mode(self):
        from ..services.guardians import match_existing

        bello = self.guardian(name="Mrs. Kemi Bello", phone=self.LANDLINE,
                              email="kemi.bello@example.ng")
        self.assertEqual(match_existing(self.tenant, phone=self.LANDLINE), bello)
        self.set_rules(matching="EMAIL_ONLY")
        self.assertIsNone(match_existing(self.tenant, phone=self.LANDLINE))
        self.assertEqual(
            match_existing(self.tenant, email="KEMI.BELLO@example.ng", phone=""), bello,
        )

    def test_the_student_import_follows_email_only(self):
        from .test_import_export import _ImportFixture

        self.set_rules(matching="EMAIL_ONLY")
        self.guardian(name="Mrs. Kemi Bello", phone=self.LANDLINE,
                      email="kemi.bello@example.ng")
        row = _ImportFixture.row(self, **{
            "Guardian Name": "Mrs. Uche Okeke", "Guardian Phone": self.LANDLINE,
            "Guardian Email": "",
        })
        issues = _import_issues(self, [row])
        self.assertFalse(
            [i for i in issues if "already uses that contact" in i["message"]], issues,
        )
        student = _execute_import_row(self, row)
        self.assertEqual(
            student.guardian_links.get().guardian.full_name, "Mrs. Uche Okeke",
        )

    def test_the_student_import_still_warns_under_email_then_phone(self):
        from .test_import_export import _ImportFixture

        self.guardian(name="Mrs. Kemi Bello", phone=self.LANDLINE,
                      email="kemi.bello@example.ng")
        issues = _import_issues(self, [_ImportFixture.row(self, **{
            "Guardian Name": "Mrs. Uche Okeke", "Guardian Phone": self.LANDLINE,
            "Guardian Email": "",
        })])
        self.assertTrue(
            [i for i in issues if "already uses that contact" in i["message"]], issues,
        )

    def test_the_guardian_import_follows_email_only(self):
        from ..guardian_imports import build_links, resolve_file

        self.set_rules(matching="EMAIL_ONLY")
        bello = self.guardian(name="Mrs. Kemi Bello", phone=self.LANDLINE,
                              email="kemi.bello@example.ng")
        child = self.student(first="Ifeoma", last="Okeke", number="BFS/9")
        resolved = resolve_file([{
            "guardian_full_name": "Mrs. Uche Okeke", "guardian_phone": self.LANDLINE,
            "guardian_email": "", "student_number": "BFS/9",
            "relationship": "Mother", "is_primary": "Yes",
        }], tenant=self.tenant)
        self.assertFalse([i for _n, i in resolved.issues if i.severity == "error"])
        build_links(resolved, tenant=self.tenant, actor=self.admin)
        linked = child.guardian_links.get().guardian
        self.assertNotEqual(linked, bello)
        self.assertEqual(linked.full_name, "Mrs. Uche Okeke")


# ── the school's own relationships ──────────────────────────────────────────

class ExtraRelationshipTests(_GuardianRulesFixture):
    """Brightfield records a Sponsor and a Driver; Sunrise records neither."""

    def setUp(self):
        self.set_rules(extra_relationships=["Sponsor", "Driver"])

    def links_of(self, user, student):
        return self.get(user, "student-guardians", pk=student.pk).data["data"]

    def test_enrolment_stores_the_schools_spelling_and_reads_its_label(self):
        response = self.post(self.admin, "student-list", self.enrolment_body(
            guardians=[self.guardian_row(relationship="sponsor")],
        ))
        self.assertEqual(response.status_code, 201, response.data)
        child = Student.all_objects.get(pk=response.data["data"]["id"])
        link = child.guardian_links.get()
        self.assertEqual((link.relationship, link.relationship_detail), ("OTHER", "Sponsor"))
        row = self.links_of(self.admin, child)[0]
        self.assertEqual((row["relationship"], row["relationship_label"]), ("OTHER", "Sponsor"))

    def test_a_fixed_relationship_reads_its_own_label(self):
        response = self.post(self.admin, "student-list", self.enrolment_body())
        child = Student.all_objects.get(pk=response.data["data"]["id"])
        row = self.links_of(self.admin, child)[0]
        self.assertEqual((row["relationship"], row["relationship_label"]), ("MOTHER", "Mother"))

    def test_a_relationship_the_school_has_not_added_is_refused(self):
        detail = self.error_detail(self.post(self.admin, "student-list", self.enrolment_body(
            guardians=[self.guardian_row(relationship="Neighbour")],
        )))
        self.assertEqual(detail["guardians"][0]["relationship"], [
            "'Neighbour' is not a relationship this school records. Pick one "
            "from the list, or add it in Settings, Guardians.",
        ])

    def test_the_link_drawer_and_the_relink_take_the_schools_relationships(self):
        child = self.student()
        created = self.post(self.admin, "student-guardians", {
            "full_name": "Mr. Sule Adamu", "phone": "08035550700",
            "relationship": "Driver", "is_primary": True,
        }, pk=child.pk)
        self.assertEqual(created.status_code, 201, created.data)
        driver = Guardian.all_objects.get(phone="08035550700")

        relinked = self.patch(
            self.admin, "student-guardian-detail", {"relationship": "SPONSOR"},
            pk=child.pk, guardian_id=driver.pk,
        )
        self.assertEqual(relinked.status_code, 200, relinked.data)
        self.assertEqual(self.links_of(self.admin, child)[0]["relationship_label"], "Sponsor")

        back = self.patch(
            self.admin, "student-guardian-detail", {"relationship": "UNCLE"},
            pk=child.pk, guardian_id=driver.pk,
        )
        self.assertEqual(back.status_code, 200, back.data)
        link = child.guardian_links.get()
        self.assertEqual((link.relationship, link.relationship_detail), ("UNCLE", ""))

        refused = self.patch(
            self.admin, "student-guardian-detail", {"relationship": "Neighbour"},
            pk=child.pk, guardian_id=driver.pk,
        )
        self.assertIn("relationship", self.error_detail(refused))
        self.assertEqual(child.guardian_links.get().relationship, "UNCLE")

    def test_another_school_cannot_use_them(self):
        child = self.student(tenant=self.solo.tenant, branch=self.solo_branch)
        refused = self.post(self.solo_admin, "student-guardians", {
            "full_name": "Mr. Sule Adamu", "phone": "08035550701",
            "relationship": "Sponsor",
        }, pk=child.pk)
        self.assertIn("relationship", self.error_detail(refused))

    def test_a_stored_label_stays_when_the_school_removes_it(self):
        response = self.post(self.admin, "student-list", self.enrolment_body(
            guardians=[self.guardian_row(relationship="Sponsor")],
        ))
        child = Student.all_objects.get(pk=response.data["data"]["id"])
        self.set_rules(extra_relationships=["Driver"])

        self.assertEqual(self.links_of(self.admin, child)[0]["relationship_label"], "Sponsor")
        guardian = child.guardian_links.get().guardian
        wards = self.get(self.admin, "guardian-detail", pk=guardian.pk).data["data"]["wards"]
        self.assertEqual(
            (wards[0]["relationship"], wards[0]["relationship_label"]), ("OTHER", "Sponsor"),
        )
        # A new link can no longer name it.
        self.assertIn("guardians", self.error_detail(self.post(
            self.admin, "student-list", self.enrolment_body(
                first_name="Musa",
                guardians=[self.guardian_row(relationship="Sponsor", phone="08035550702",
                                             email="musa@example.ng")],
            ),
        )))

    def test_the_student_import_recognises_them_and_imports_others_as_other(self):
        from .test_import_export import _ImportFixture

        sponsored = _ImportFixture.row(self, **{"Guardian Relationship": "sponsor"})
        issues = _import_issues(self, [sponsored])
        self.assertFalse(
            [i for i in issues if i["column_name"] == "Guardian Relationship"], issues,
        )
        link = _execute_import_row(self, sponsored).guardian_links.get()
        self.assertEqual((link.relationship, link.relationship_detail), ("OTHER", "Sponsor"))

        friend = _ImportFixture.row(self, **{
            "First Name": "Tobi", "Guardian Relationship": "Family friend",
        })
        warnings = [
            i for i in _import_issues(self, [friend])
            if i["column_name"] == "Guardian Relationship"
        ]
        self.assertEqual([w["severity"] for w in warnings], ["warning"])
        link = _execute_import_row(self, friend).guardian_links.get(
            student__first_name="Tobi",
        )
        self.assertEqual((link.relationship, link.relationship_detail), ("OTHER", ""))

    def test_the_guardian_import_recognises_them_and_reports_the_label(self):
        from ..guardian_imports import execute_guardians_import

        child = self.student(number="BFS/1")
        batch = _guardian_batch(self, [_guardian_row(Relationship="driver")])
        self.assertFalse(
            [i for i in _guardian_import_issues(self, [_guardian_row(Relationship="driver")])
             if i["column_name"] == "Relationship"],
        )
        job = execute_guardians_import(batch, self.admin)
        link = StudentGuardian.all_objects.get(student=child)
        self.assertEqual((link.relationship, link.relationship_detail), ("OTHER", "Driver"))
        result = job.row_results.get()
        self.assertEqual(result.normalized_payload["relationship_label"], "Driver")

        stranger = [
            i for i in _guardian_import_issues(self, [_guardian_row(Relationship="Neighbour")])
            if i["column_name"] == "Relationship"
        ]
        self.assertEqual([i["severity"] for i in stranger], ["warning"])


# ── the catalogue and the templates ─────────────────────────────────────────

class GuardianSettingsCatalogueTests(_GuardianRulesFixture):

    def test_every_key_exists_with_todays_default_and_a_school_scope(self):
        from vs_config.models import ConfigurationDefinition

        expected = {
            "guardians.min_per_student": 1,
            "guardians.email_required": False,
            "guardians.matching": "EMAIL_THEN_PHONE",
            "guardians.relationships.extra": [],
        }
        for key, default in expected.items():
            with self.subTest(key=key):
                row = ConfigurationDefinition.objects.get(key=key)
                self.assertEqual(row.default_value, default)
                self.assertEqual(sorted(row.allowed_scopes), ["platform", "school"])

    def test_a_bad_stored_value_costs_the_school_its_rule_not_an_enrolment(self):
        """Values stored by hand at the platform layer are cleaned on read."""
        from vs_config.models import ConfigurationDefinition, ConfigurationValue

        from ..services.guardian_rules import read_guardian_rules

        for key, value in (
            ("guardians.min_per_student", 9),
            ("guardians.matching", "PHONE_ONLY"),
            ("guardians.relationships.extra", ["Sponsor", "sponsor", "Mother", "", 7]),
        ):
            ConfigurationValue.all_objects.create(
                definition=ConfigurationDefinition.objects.get(key=key),
                scope_key="platform", value=value,
            )
        rules = read_guardian_rules(self.tenant)
        self.assertEqual(rules.min_per_student, 1)
        self.assertEqual(rules.matching, "EMAIL_THEN_PHONE")
        self.assertEqual(rules.extra_relationships, ("Sponsor",))

    def test_both_templates_state_the_rules_as_every_school_may_set_them(self):
        from vs_import_data.models import ImportTemplate, ImportTemplateColumn

        for code, relationship_field in (
            ("students_v1", "guardian_relationship"), ("guardians_v1", "relationship"),
        ):
            with self.subTest(code=code):
                template = ImportTemplate.objects.get(code=code)
                self.assertIn("or on email alone", template.instructions)
                self.assertIn("your school has added in Settings, Guardians",
                              template.instructions)
                self.assertIn("where your school requires", template.instructions)
                columns = {
                    c.target_field: c.help_text
                    for c in ImportTemplateColumn.objects.filter(template=template)
                }
                self.assertIn("added in Settings, Guardians", columns[relationship_field])
                self.assertIn("email alone", columns["guardian_phone"])
                self.assertIn("requires a guardian email", columns["guardian_email"])
        students = ImportTemplate.objects.get(code="students_v1")
        self.assertIn("more than one guardian per child", students.instructions)


# ── helpers shared by the classes above ─────────────────────────────────────

def _import_batch(case, rows):
    from vs_import_data.models import ImportBatch, ImportTemplate

    return ImportBatch.all_objects.create(
        tenant=case.tenant, branch=None,
        template=ImportTemplate.objects.get(code="students_v1"),
        dataset_type="students", preview_rows=rows,
        original_filename="roll.xlsx", uploaded_by=case.admin,
    )


def _import_issues(case, rows):
    from ..imports import validate_students_import_batch

    return validate_students_import_batch(_import_batch(case, rows))


def _execute_import_row(case, raw_row):
    from vs_import_data.services.import_executor import (
        execute_dataset_handler,
        map_row_to_payload,
    )

    batch = _import_batch(case, [raw_row])
    return execute_dataset_handler(
        batch, map_row_to_payload(batch, raw_row), case.admin,
    ).instance


def _guardian_row(**overrides):
    row = {
        "Guardian Name": "Mr. Emeka Adeleke", "Guardian Phone": "08035550102",
        "Guardian Email": "emeka@example.ng", "Admission Number": "BFS/1",
        "Student First Name": "", "Student Last Name": "",
        "Student Date of Birth": "", "Relationship": "Father",
        "Primary Contact": "Yes", "Occupation": "", "Home Address": "",
    }
    row.update(overrides)
    return row


def _guardian_batch(case, rows):
    from vs_import_data.models import ImportBatch, ImportTemplate

    return ImportBatch.all_objects.create(
        tenant=case.tenant,
        template=ImportTemplate.objects.get(code="guardians_v1"),
        dataset_type="guardians", preview_rows=rows,
        original_filename="households.xlsx", uploaded_by=case.admin,
    )


def _guardian_import_issues(case, rows):
    from ..guardian_imports import validate_guardians_import_batch

    return validate_guardians_import_batch(_guardian_batch(case, rows))
