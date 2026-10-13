"""Who takes which non-core subject: the rules, then the endpoint.

The service rules are tested by calling them. The endpoint tests cover what only
the API layer does: the key, the class teacher rule, which classes a caller can
open, and the shape of the answer. Both shapes of school are exercised where
branch matters: Brightfield has two branches and Sunrise has one.
"""
from __future__ import annotations

from vs_audit.models import AuditEvent

from ..exceptions import ElectiveRefused, ElectiveRemovalRefused
from ..models import ElectiveTaker
from ..services import electives
from .electives_base import TODAY, ElectivesFixture


def _record(tenant, school_class, term, subject, student, actor=None):
    return ElectiveTaker.all_objects.create(
        tenant=tenant, school_class=school_class, term=term, subject=subject,
        student=student, set_by=actor,
    )


class ServiceRuleTests(ElectivesFixture):
    def test_an_offering_overrides_its_subject_in_both_directions(self):
        offerings = {
            o.subject_id: o for o in
            self.jss1.subject_offerings.select_related("subject")
        }
        self.assertTrue(electives.effective_is_core(offerings[self.maths.pk]))
        # Civic is an elective subject, core at this level through its offering.
        self.assertTrue(electives.effective_is_core(offerings[self.civic.pk]))
        # French is a core subject, an elective at this level through its offering.
        self.assertFalse(electives.effective_is_core(offerings[self.french.pk]))
        self.assertFalse(electives.effective_is_core(offerings[self.yoruba.pk]))

    def test_the_non_core_offerings_are_the_active_electives_of_the_level_by_name(self):
        names = [o.subject.name for o in electives.non_core_offerings(self.jss1a)]
        self.assertEqual(names, ["French", "Yoruba"])
        self.assertEqual(electives.non_core_offerings(self.jss2a), [])

    def test_takers_are_grouped_by_subject_for_the_class_and_term_only(self):
        _record(self.tenant, self.jss1a, self.first_term, self.yoruba, self.tunde)
        _record(self.tenant, self.jss1a, self.first_term, self.yoruba, self.kelechi)
        _record(self.tenant, self.jss1a, self.first_term, self.french, self.tunde)
        _record(self.tenant, self.jss1a, self.second_term, self.french, self.chiamaka)
        _record(self.tenant, self.jss1b, self.first_term, self.french, self.sade)
        self.assertEqual(
            electives.takers(self.tenant, self.jss1a, self.first_term),
            {
                self.yoruba.pk: {self.tunde.pk, self.kelechi.pk},
                self.french.pk: {self.tunde.pk},
            },
        )

    def test_a_core_subject_is_everyones_and_an_elective_only_its_takers(self):
        _record(self.tenant, self.jss1a, self.first_term, self.yoruba, self.tunde)
        args = (self.tenant, self.jss1a, self.first_term)
        self.assertTrue(electives.takes(*args, self.maths, self.chiamaka.pk))
        self.assertTrue(electives.takes(*args, self.civic.pk, self.chiamaka.pk))
        self.assertTrue(electives.takes(*args, self.yoruba, self.tunde.pk))
        self.assertFalse(electives.takes(*args, self.yoruba, self.chiamaka.pk))
        self.assertFalse(electives.takes(*args, self.french, self.tunde.pk))
        # A choice recorded in another term is not this term's.
        self.assertFalse(
            electives.takes(self.tenant, self.jss1a, self.second_term, self.yoruba, self.tunde.pk),
        )

    def test_a_subject_the_level_does_not_offer_is_taken_by_nobody(self):
        self.assertFalse(
            electives.takes(self.tenant, self.jss2a, self.first_term, self.yoruba, self.jss2_pupil.pk),
        )

    def test_the_default_term_is_the_one_running_then_the_latest_begun_then_the_first(self):
        terms = [self.first_term, self.second_term]
        self.assertEqual(electives.default_term(terms, TODAY), self.first_term)
        self.assertEqual(
            electives.default_term(terms, self.second_term.start_date), self.second_term,
        )
        between = self.first_term.end_date.replace(day=20)
        self.assertEqual(electives.default_term(terms, between), self.first_term)
        before = self.first_term.start_date.replace(day=1)
        self.assertEqual(electives.default_term(terms, before), self.first_term)
        self.assertIsNone(electives.default_term([], TODAY))


class RemovalGuardTests(ElectivesFixture):
    def setUp(self):
        super().setUp()
        self.guards = list(electives.REMOVAL_GUARDS)
        self.addCleanup(lambda: electives.REMOVAL_GUARDS.__setitem__(slice(None), self.guards))

    def apply(self, *changes):
        return electives.apply_changes(
            self.tenant, self.jss1a, self.first_term, list(changes),
            actor=self.admin, today=TODAY,
        )

    def test_a_guard_that_refuses_a_removal_refuses_the_whole_request_in_its_words(self):
        _record(self.tenant, self.jss1a, self.first_term, self.yoruba, self.tunde)
        _record(self.tenant, self.jss1a, self.first_term, self.yoruba, self.kelechi)
        calls = []

        def guard(tenant, school_class, term, subject, student_id):
            calls.append((school_class.pk, term.pk, subject.pk, student_id))
            return "Tunde already has marks in Yoruba." if student_id == self.tunde.pk else None

        electives.register_removal_guard(guard)
        electives.register_removal_guard(guard)
        self.assertEqual(electives.REMOVAL_GUARDS.count(guard), 1)

        with self.assertRaises(ElectiveRemovalRefused) as raised:
            self.apply(
                self.change(self.kelechi, self.yoruba, False),
                self.change(self.tunde, self.yoruba, False),
            )
        self.assertEqual(raised.exception.message, "Tunde already has marks in Yoruba.")
        self.assertEqual(ElectiveTaker.objects.filter(subject=self.yoruba).count(), 2)
        self.assertIn(
            (self.jss1a.pk, self.first_term.pk, self.yoruba.pk, self.tunde.pk), calls,
        )

    def test_a_guard_is_asked_only_about_removing_a_recorded_choice(self):
        calls = []
        electives.register_removal_guard(lambda *args: calls.append(args))
        # Adding, and removing a choice nobody recorded, ask nobody.
        self.apply(
            self.change(self.tunde, self.yoruba, True),
            self.change(self.kelechi, self.french, False),
        )
        self.assertEqual(calls, [])
        self.apply(self.change(self.tunde, self.yoruba, False))
        self.assertEqual(len(calls), 1)


class ElectivesSecurityTests(ElectivesFixture):
    def test_a_caller_without_the_view_key_is_refused(self):
        self.assertEqual(self.read(self.nobody, self.jss1a).status_code, 403)

    def test_a_caller_with_only_the_view_key_reads_but_cannot_save(self):
        self.assertEqual(self.read(self.viewer, self.jss1a).status_code, 200)
        refused = self.save(self.viewer, self.jss1a, [self.change(self.tunde, self.yoruba)])
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(ElectiveTaker.objects.exists())

    def test_another_schools_class_is_a_404_to_read_and_to_save(self):
        self.assertEqual(self.read(self.admin, self.solo_class).status_code, 404)
        refused = self.save(
            self.admin, self.solo_class, [self.change(self.solo_pupil, self.solo_subject)],
        )
        self.assertEqual(refused.status_code, 404)
        self.assertFalse(ElectiveTaker.all_objects.exists())

    def test_another_schools_term_is_a_404(self):
        self.assertEqual(self.read(self.admin, self.jss1a, self.solo_term).status_code, 404)

    def test_a_class_in_a_branch_the_caller_cannot_see_is_a_404(self):
        self.assertEqual(self.read(self.ikeja_admin, self.jss1a).status_code, 404)
        refused = self.save(self.ikeja_admin, self.jss1a, [self.change(self.tunde, self.yoruba)])
        self.assertEqual(refused.status_code, 404)
        self.assertEqual(self.read(self.lekki_admin, self.jss1i).status_code, 404)
        self.assertFalse(ElectiveTaker.objects.exists())

    def test_a_teacher_who_does_not_lead_the_class_reads_it_but_cannot_save(self):
        body = self.data(self.read(self.bello, self.jss1a))
        self.assertFalse(body["can_edit"])
        refused = self.save(self.bello, self.jss1a, [self.change(self.tunde, self.yoruba)])
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.data["error"]["code"], "NOT_YOUR_CLASS")
        self.assertIn("class teacher or an administrator", refused.data["message"])
        self.assertFalse(ElectiveTaker.objects.exists())

    def test_the_class_teacher_saves_their_own_class(self):
        self.assertTrue(self.data(self.read(self.eze, self.jss1a))["can_edit"])
        saved = self.save(self.eze, self.jss1a, [self.change(self.tunde, self.yoruba)])
        self.assertEqual(saved.status_code, 200, saved.data)
        row = ElectiveTaker.objects.get()
        self.assertEqual(row.set_by_id, self.eze.pk)
        self.assertEqual(row.tenant_id, self.tenant.pk)

    def test_the_class_teacher_of_one_class_cannot_save_another(self):
        refused = self.save(self.eze, self.jss1b, [self.change(self.sade, self.yoruba)])
        self.assertEqual(refused.status_code, 403)

    def test_a_class_teacher_who_has_left_the_roll_is_no_longer_one(self):
        self.eze_staff.employment_status = "RESIGNED"
        self.eze_staff.save(update_fields=["employment_status"])
        refused = self.save(self.eze, self.jss1a, [self.change(self.tunde, self.yoruba)])
        self.assertEqual(refused.status_code, 403)

    def test_an_administrator_saves_any_class_of_their_branches(self):
        for admin in (self.admin, self.lekki_admin):
            saved = self.save(admin, self.jss1b, [self.change(self.sade, self.yoruba)])
            self.assertEqual(saved.status_code, 200, saved.data)

    def test_a_branch_bound_administrator_cannot_save_a_class_shared_by_every_branch(self):
        # Readable, because a null branch means every branch, but not theirs to set.
        self.assertEqual(self.read(self.lekki_admin, self.shared).status_code, 200)
        refused = self.save(self.lekki_admin, self.shared, [])
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.data["error"]["code"], "NOT_YOUR_CLASS")

    def test_at_a_one_branch_school_the_class_teacher_and_the_administrator_both_save(self):
        from core.test_utils import TenantAPIClient

        change = {"student": self.solo_pupil.pk, "subject": self.solo_subject.pk, "takes": True}
        for user in (self.solo_teacher, self.solo_admin):
            body = {"changes": [change]}
            saved = TenantAPIClient(user).put(self.url(self.solo_class), body, format="json")
            self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(ElectiveTaker.all_objects.filter(tenant=self.solo).count(), 1)


class ElectivesReadShapeTests(ElectivesFixture):
    def test_the_screen_lists_the_electives_and_the_whole_roll_in_name_order(self):
        _record(self.tenant, self.jss1a, self.first_term, self.yoruba, self.tunde)
        _record(self.tenant, self.jss1a, self.first_term, self.yoruba, self.amina)
        _record(self.tenant, self.jss1a, self.first_term, self.french, self.tunde)
        response = self.read(self.admin, self.jss1a)
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)

        self.assertEqual(body["class"], {"id": self.jss1a.pk, "name": "JSS1 A"})
        self.assertEqual(body["term"], {"id": self.first_term.pk, "name": "First Term"})
        self.assertEqual(
            body["subjects"],
            [
                {"id": self.french.pk, "name": "French", "code": "FRN", "takers": 1, "roll": 4},
                {"id": self.yoruba.pk, "name": "Yoruba", "code": "YOR", "takers": 2, "roll": 4},
            ],
        )
        # Amina is suspended and still on the roll; Obinna is placed later.
        self.assertEqual(
            [s["name"] for s in body["students"]],
            ["Tunde Bello", "Kelechi Eze", "Chiamaka Nwosu", "Amina Yusuf"],
        )
        tunde = body["students"][0]
        self.assertEqual(
            tunde,
            {
                "id": self.tunde.pk, "name": "Tunde Bello", "admission_number": "BFS/002",
                "takes": sorted([self.french.pk, self.yoruba.pk]),
            },
        )
        self.assertEqual(body["students"][3]["takes"], [self.yoruba.pk])
        self.assertEqual(body["students"][1]["takes"], [])

    def test_a_class_with_no_elective_has_an_empty_subject_list_and_still_a_roll(self):
        body = self.data(self.read(self.admin, self.jss2a))
        self.assertEqual(body["subjects"], [])
        self.assertEqual([s["name"] for s in body["students"]], ["Ife Adeola"])
        self.assertEqual(body["students"][0]["takes"], [])

    def test_a_class_with_nobody_on_it_has_an_empty_roll(self):
        body = self.data(self.read(self.admin, self.shared))
        self.assertEqual(body["students"], [])
        self.assertEqual([s["roll"] for s in body["subjects"]], [0, 0])

    def test_without_a_term_the_current_one_is_shown_and_a_named_term_is_honoured(self):
        self.assertEqual(self.data(self.read(self.admin, self.jss1a))["term"]["id"], self.first_term.pk)
        named = self.data(self.read(self.admin, self.jss1a, self.second_term))
        self.assertEqual(named["term"]["id"], self.second_term.pk)
        self.assertEqual(named["subjects"][0]["takers"], 0)

    def test_a_term_of_another_year_is_refused_in_words(self):
        response = self.read(self.admin, self.jss1a, self.old_term)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "ELECTIVE_REFUSED")
        self.assertIn("2025/2026", response.data["message"])

    def test_the_roll_of_a_finished_term_is_read_as_at_its_last_day(self):
        # Obinna joined on 3 November, inside First Term, which runs to 12 December.
        late = self.clock_at(self.second_term.start_date)
        with late:
            body = self.data(self.read(self.admin, self.jss1a, self.first_term))
        self.assertIn("Obinna Okafor", [s["name"] for s in body["students"]])
        self.assertEqual(body["subjects"][0]["roll"], 5)

    def clock_at(self, day):
        import datetime as dt

        from .electives_base import school_clock

        return school_clock(dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.timezone.utc))

    def test_a_reader_sees_the_same_roll_at_a_one_branch_school(self):
        from core.test_utils import TenantAPIClient

        body = self.data(TenantAPIClient(self.solo_admin).get(self.url(self.solo_class)))
        self.assertEqual([s["name"] for s in body["subjects"]], ["Arabic"])
        self.assertEqual([s["name"] for s in body["students"]], ["Ebube Okoro"])
        self.assertNotIn("branch", body["class"])


class ElectivesWriteTests(ElectivesFixture):
    def test_a_save_records_the_choices_and_answers_with_the_screen(self):
        saved = self.save(self.admin, self.jss1a, [
            self.change(self.tunde, self.yoruba),
            self.change(self.kelechi, self.yoruba),
            self.change(self.tunde, self.french),
        ])
        self.assertEqual(saved.status_code, 200, saved.data)
        body = self.data(saved)
        self.assertEqual({s["name"]: s["takers"] for s in body["subjects"]}, {"French": 1, "Yoruba": 2})
        self.assertEqual(ElectiveTaker.objects.count(), 3)

    def test_saving_the_same_choice_twice_changes_nothing_and_a_removal_removes_it(self):
        self.save(self.admin, self.jss1a, [self.change(self.tunde, self.yoruba)])
        again = self.save(self.admin, self.jss1a, [self.change(self.tunde, self.yoruba)])
        self.assertEqual(again.status_code, 200)
        self.assertEqual(ElectiveTaker.objects.count(), 1)
        removed = self.save(self.admin, self.jss1a, [self.change(self.tunde, self.yoruba, False)])
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(ElectiveTaker.objects.count(), 0)

    def test_a_pair_named_twice_takes_its_last_value(self):
        self.save(self.admin, self.jss1a, [
            self.change(self.tunde, self.yoruba, True),
            self.change(self.tunde, self.yoruba, False),
        ])
        self.assertEqual(ElectiveTaker.objects.count(), 0)

    def test_choices_belong_to_their_term(self):
        self.save(self.admin, self.jss1a, [self.change(self.tunde, self.yoruba)], term=self.second_term)
        self.assertEqual(ElectiveTaker.objects.get().term_id, self.second_term.pk)
        first = self.data(self.read(self.admin, self.jss1a, self.first_term))
        self.assertEqual([s["takers"] for s in first["subjects"]], [0, 0])

    def test_a_core_subject_is_refused_whether_core_by_subject_or_by_offering(self):
        for subject in (self.maths, self.civic):
            response = self.save(self.admin, self.jss1a, [self.change(self.tunde, subject)])
            self.assertEqual(response.status_code, 422, response.data)
            self.assertEqual(response.data["error"]["code"], "ELECTIVE_REFUSED")
            self.assertIn("core subject", response.data["message"])
            self.assertEqual(response.data["error"]["detail"]["index"], 0)
        self.assertFalse(ElectiveTaker.objects.exists())

    def test_a_subject_the_level_does_not_offer_or_has_archived_is_refused(self):
        for subject in (self.chemistry, self.solo_subject):
            response = self.save(self.admin, self.jss1a, [self.change(self.tunde, subject)])
            self.assertEqual(response.status_code, 422, response.data)
            self.assertIn("not an elective at JSS1", response.data["message"])
        # JSS2 offers Yoruba nowhere.
        response = self.save(self.admin, self.jss2a, [self.change(self.jss2_pupil, self.yoruba)])
        self.assertEqual(response.status_code, 422)

    def test_a_student_not_on_the_roll_is_refused(self):
        for student in (self.obinna, self.sade, self.solo_pupil):
            response = self.save(self.admin, self.jss1a, [self.change(student, self.yoruba)])
            self.assertEqual(response.status_code, 422, response.data)
            self.assertIn("not on the JSS1 A roll", response.data["message"])
            # The refusal never names a student who is not on the roll.
            self.assertNotIn(student.first_name, response.data["message"])
        self.assertFalse(ElectiveTaker.all_objects.exists())

    def test_a_suspended_student_is_on_the_roll_and_can_be_recorded(self):
        saved = self.save(self.admin, self.jss1a, [self.change(self.amina, self.yoruba)])
        self.assertEqual(saved.status_code, 200, saved.data)

    def test_a_term_outside_the_classs_year_is_refused(self):
        response = self.save(
            self.admin, self.jss1a, [self.change(self.tunde, self.yoruba)], term=self.old_term,
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ElectiveTaker.objects.exists())

    def test_one_bad_row_changes_nothing(self):
        response = self.save(self.admin, self.jss1a, [
            self.change(self.tunde, self.yoruba),
            self.change(self.kelechi, self.maths),
        ])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["detail"]["index"], 1)
        self.assertFalse(ElectiveTaker.objects.exists())

    def test_a_save_with_no_changes_or_a_malformed_body_is_a_validation_error(self):
        self.assertEqual(self.save(self.admin, self.jss1a, []).status_code, 400)
        from core.test_utils import TenantAPIClient

        client = TenantAPIClient(self.admin)
        self.assertEqual(client.put(self.url(self.jss1a), {}, format="json").status_code, 400)
        bad = {"changes": [{"student": self.tunde.pk, "subject": self.yoruba.pk}]}
        self.assertEqual(client.put(self.url(self.jss1a), bad, format="json").status_code, 400)

    def test_a_save_that_changes_something_is_audited_once_and_a_no_op_is_not(self):
        self.save(self.eze, self.jss1a, [
            self.change(self.tunde, self.yoruba), self.change(self.kelechi, self.yoruba),
        ])
        events = AuditEvent.objects.filter(entity_type="SchoolClass", entity_id=str(self.jss1a.pk))
        [event] = list(events.filter(summary__icontains="who takes what"))
        self.assertEqual(event.actor_user_id, self.eze.pk)
        self.assertIn("2 added, 0 removed", event.summary)

        self.save(self.eze, self.jss1a, [self.change(self.tunde, self.yoruba)])
        self.assertEqual(events.filter(summary__icontains="who takes what").count(), 1)
