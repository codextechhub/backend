"""A school's applicant rules: its own admission stages, offers, and documents to confirm.

Security first: who may read and change the rules, who may move an applicant,
and that one school's stages never reach another. Then the refusals of the
settings save, the offer dates against the school's own clock, the directory's
stage filter, and the documents a confirmation waits for. Brightfield has two
branches and Sunrise one, so every rule is seen at both shapes of school, and a
school that names no stages is held to behave exactly as every school did.

The clock is patched at ``vs_config.clock.tenant_now``, the one place every
"today" at a school is read from, never at the server's date.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import tag
from django.test.utils import CaptureQueriesContext

from core.migration_testing import RewoundSchemaTestCase
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

from ..constants import DocumentType, StudentStatus
from ..models import AdmissionStage, Student, StudentDocument
from .base import StudentsFixture

LAGOS = ZoneInfo("Africa/Lagos")


@contextmanager
def school_day(year, month, day):
    """Every school's today is *year-month-day*, mid-morning in Lagos."""
    moment = dt.datetime(year, month, day, 10, 0, tzinfo=LAGOS)
    with mock.patch("vs_config.clock.tenant_now", return_value=moment):
        yield dt.date(year, month, day)


class _AdmissionFixture(StudentsFixture):
    """The shared fixture plus a settings admin at each school."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        settings_key = make_permission(
            "school.settings.update", scope=PermissionScope.TENANT,
        )
        view_key = cls.permissions["school.students.view"]

        # Holds the settings key and may read students, but not update them.
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

    # ── helpers ────────────────────────────────────────────────────────────

    def rules_body(self, stages=(), documents=(), **extra):
        return {
            "stages": list(stages),
            "required_documents_to_confirm": list(documents),
            **extra,
        }

    def put_rules(self, stages=(), documents=(), user=None, **extra):
        return self.put(
            user or self.settings_admin, "student-admission-rules",
            self.rules_body(stages, documents, **extra),
        )

    def set_rules(self, stages=(), documents=(), user=None):
        response = self.put_rules(stages, documents, user=user)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def standard_stages(self, user=None):
        """Entrance exam, Interview, Offer (14 days), Accepted."""
        data = self.set_rules([
            {"name": "Entrance exam", "is_offer": False},
            {"name": "Interview", "is_offer": False},
            {"name": "Offer", "is_offer": True, "offer_valid_days": 14},
            {"name": "Accepted", "is_offer": False},
        ], user=user)
        return {row["name"]: row["id"] for row in data["stages"]}

    def applicant(self, *, branch=None, tenant=None, first="Tunde", **extra):
        return self.student(
            branch=branch, tenant=tenant, first=first, last="Bello",
            status=StudentStatus.APPLICANT, **extra,
        )

    def move(self, student, stage, user=None, **body):
        return self.post(
            user or self.admin, "student-stage", {"stage": stage, **body},
            pk=student.pk,
        )

    def refusal(self, response, status=400):
        self.assertEqual(response.status_code, status, response.data)
        return response.data["error"]["detail"]

    def attach(self, student, document_type=DocumentType.BIRTH_CERTIFICATE):
        return StudentDocument.all_objects.create(
            tenant=student.tenant, student=student, document_type=document_type,
            file=SimpleUploadedFile("doc.pdf", b"%PDF-1", content_type="application/pdf"),
            original_name="doc.pdf",
        )


# ── security ────────────────────────────────────────────────────────────────

class AdmissionRulesSecurityTests(_AdmissionFixture):

    def test_reading_the_rules_needs_the_students_view_key(self):
        self.assertEqual(self.get(self.nobody, "student-admission-rules").status_code, 403)
        self.assertEqual(self.get(self.admin, "student-admission-rules").status_code, 200)

    def test_changing_the_rules_needs_the_settings_key_not_a_student_key(self):
        """Adaeze holds every student key and still may not change the rules."""
        body = self.rules_body([{"name": "Interview"}])
        self.assertEqual(
            self.put(self.admin, "student-admission-rules", body).status_code, 403,
        )
        self.assertEqual(
            self.put(self.nobody, "student-admission-rules", body).status_code, 403,
        )
        self.assertFalse(AdmissionStage.all_objects.filter(tenant=self.tenant).exists())
        self.set_rules([{"name": "Interview"}])

    def test_moving_an_applicant_needs_the_key_that_confirms_one(self):
        """school.students.update: the settings admin reads students but holds it not."""
        stages = self.standard_stages()
        tunde = self.applicant()
        for user in (self.nobody, self.settings_admin):
            with self.subTest(user=user.email):
                self.assertEqual(self.move(tunde, stages["Interview"], user=user).status_code, 403)
        tunde.refresh_from_db()
        self.assertIsNone(tunde.admission_stage_id)
        self.assertEqual(self.move(tunde, stages["Interview"]).status_code, 200)

    def test_one_schools_stages_never_reach_another(self):
        self.standard_stages()
        theirs = self.get(self.solo_admin, "student-admission-rules").data["data"]
        self.assertEqual(theirs["stages"], [])
        self.assertEqual(theirs["required_documents_to_confirm"], [])

    def test_another_schools_stage_is_a_404_on_the_move(self):
        solo_stage = AdmissionStage.all_objects.create(
            tenant=self.solo.tenant, name="Assessment", position=1,
        )
        tunde = self.applicant()
        response = self.move(tunde, solo_stage.pk)
        self.assertEqual(response.status_code, 404, response.data)
        tunde.refresh_from_db()
        self.assertIsNone(tunde.admission_stage_id)

    def test_another_schools_stage_cannot_be_updated_or_removed_by_id(self):
        """Sunrise names Brightfield's stage id in its save; nothing of Brightfield's moves."""
        stages = self.standard_stages()
        detail = self.refusal(self.put_rules(
            [{"id": stages["Offer"], "name": "Hijacked", "is_offer": False}],
            user=self.solo_settings,
        ))
        self.assertIn("not one of this school's admission stages", str(detail["stages"]))
        self.assertEqual(
            AdmissionStage.all_objects.get(pk=stages["Offer"]).name, "Offer",
        )

    def test_another_schools_student_is_a_404_on_the_move(self):
        stages = self.standard_stages()
        stranger = self.applicant(tenant=self.solo.tenant, branch=self.solo_branch)
        self.assertEqual(self.move(stranger, stages["Interview"]).status_code, 404)

    def test_the_stage_filter_refuses_another_schools_stage(self):
        solo_stage = AdmissionStage.all_objects.create(
            tenant=self.solo.tenant, name="Assessment", position=1,
        )
        detail = self.refusal(self.get(self.admin, "student-list", {"stage": solo_stage.pk}))
        self.assertEqual(detail["stage"], ["No such admission stage at this school."])

    def test_a_branch_head_counts_only_their_branchs_applicants(self):
        stages = self.standard_stages()
        for i in range(2):
            self.move(self.applicant(branch=self.lekki, first=f"Lekki{i}"), stages["Interview"])
        self.move(self.applicant(branch=self.ikeja, first="Ikeja"), stages["Interview"])

        def interview(user, params=None):
            rows = self.get(user, "student-admission-rules", params).data["data"]["stages"]
            return next(r for r in rows if r["name"] == "Interview")["applicants"]

        self.assertEqual(interview(self.admin), 3)
        self.assertEqual(interview(self.lekki_head), 2)
        self.assertEqual(interview(self.admin, {"branch": self.ikeja.pk}), 1)


# ── the settings save ───────────────────────────────────────────────────────

class AdmissionRulesShapeTests(_AdmissionFixture):

    def test_a_school_that_has_set_nothing_has_no_stages_and_no_documents(self):
        data = self.get(self.admin, "student-admission-rules").data["data"]
        self.assertEqual(data["stages"], [])
        self.assertEqual(data["required_documents_to_confirm"], [])
        self.assertEqual(
            data["document_types"],
            [{"value": v, "label": l} for v, l in DocumentType.choices],
        )
        enrolment = self.get(self.admin, "student-enrolment-rules").data["data"]
        self.assertEqual(data["document_types"], enrolment["document_types"])

    def test_the_list_order_is_the_position_and_the_body_is_the_get_body(self):
        response = self.put_rules([
            {"name": " Entrance exam ", "is_offer": False},
            {"name": "Offer", "is_offer": True, "offer_valid_days": 14},
        ], ["BIRTH_CERTIFICATE"])
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(
            [(r["name"], r["position"], r["is_offer"], r["offer_valid_days"], r["applicants"])
             for r in data["stages"]],
            [("Entrance exam", 1, False, None, 0), ("Offer", 2, True, 14, 0)],
        )
        self.assertEqual(data["required_documents_to_confirm"], ["BIRTH_CERTIFICATE"])
        self.assertEqual(data, self.get(self.admin, "student-admission-rules").data["data"])

    def test_an_id_updates_a_missing_stage_is_removed_and_a_new_one_created(self):
        stages = self.standard_stages()
        data = self.set_rules([
            {"id": stages["Offer"], "name": "Offer made", "is_offer": True, "offer_valid_days": 7},
            {"id": stages["Entrance exam"], "name": "Entrance exam", "is_offer": False},
            {"name": "Deposit paid", "is_offer": False},
        ])
        self.assertEqual(
            [(r["name"], r["position"]) for r in data["stages"]],
            [("Offer made", 1), ("Entrance exam", 2), ("Deposit paid", 3)],
        )
        self.assertEqual(data["stages"][0]["id"], stages["Offer"])
        self.assertEqual(data["stages"][0]["offer_valid_days"], 7)
        self.assertFalse(AdmissionStage.all_objects.filter(
            pk__in=[stages["Interview"], stages["Accepted"]],
        ).exists())

    def test_two_stages_can_swap_names_in_one_save(self):
        stages = self.standard_stages()
        data = self.set_rules([
            {"id": stages["Entrance exam"], "name": "Interview"},
            {"id": stages["Interview"], "name": "Entrance exam"},
            {"id": stages["Offer"], "name": "Offer", "is_offer": True, "offer_valid_days": 14},
            {"id": stages["Accepted"], "name": "Accepted"},
        ])
        names = {r["id"]: r["name"] for r in data["stages"]}
        self.assertEqual(names[stages["Entrance exam"]], "Interview")
        self.assertEqual(names[stages["Interview"]], "Entrance exam")

    def test_a_removed_stage_leaves_its_confirmed_records_with_no_stage(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        self.move(tunde, stages["Interview"])
        Student.all_objects.filter(pk=tunde.pk).update(status=StudentStatus.ENROLLED)
        self.set_rules([{"id": stages["Offer"], "name": "Offer", "is_offer": True,
                         "offer_valid_days": 14}])
        tunde.refresh_from_db()
        self.assertIsNone(tunde.admission_stage_id)

    def test_every_change_is_audited_and_an_unchanged_save_writes_nothing(self):
        from vs_audit.models import AuditEvent
        from vs_config.models import ConfigurationAuditEvent

        stages = self.standard_stages()
        self.assertEqual(
            AuditEvent.objects.filter(tenant=self.tenant, entity_type="AdmissionStage",
                                      action_type="CREATE").count(), 4,
        )
        body = [
            {"id": stages[name], "name": name, "is_offer": name == "Offer",
             "offer_valid_days": 14 if name == "Offer" else None}
            for name in ("Entrance exam", "Interview", "Offer", "Accepted")
        ]
        self.set_rules(body, ["BIRTH_CERTIFICATE"])
        stage_events = AuditEvent.objects.filter(entity_type="AdmissionStage").count()
        config_events = ConfigurationAuditEvent.objects.count()

        self.set_rules(body, ["BIRTH_CERTIFICATE"])
        self.assertEqual(AuditEvent.objects.filter(entity_type="AdmissionStage").count(), stage_events)
        self.assertEqual(ConfigurationAuditEvent.objects.count(), config_events)

        body[1]["name"] = "Panel interview"
        self.set_rules(body[:3], ["BIRTH_CERTIFICATE"])
        renamed = AuditEvent.objects.get(entity_type="AdmissionStage", action_type="UPDATE",
                                         entity_id=str(stages["Interview"]))
        self.assertEqual(renamed.diff_data["name"], {"from": "Interview", "to": "Panel interview"})
        self.assertTrue(AuditEvent.objects.filter(
            entity_type="AdmissionStage", action_type="DELETE",
            entity_id=str(stages["Accepted"]),
        ).exists())

    def test_a_single_branch_school_sets_its_own(self):
        stages = self.standard_stages(user=self.solo_settings)
        amaka = self.applicant(tenant=self.solo.tenant, branch=self.solo_branch, first="Amaka")
        response = self.move(amaka, stages["Interview"], user=self.solo_admin)
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["data"]
        self.assertNotIn("branch", row)
        self.assertEqual(row["admission_stage_name"], "Interview")
        rows = self.get(self.solo_admin, "student-admission-rules").data["data"]["stages"]
        self.assertEqual(next(r for r in rows if r["name"] == "Interview")["applicants"], 1)
        self.assertEqual(self.get(self.admin, "student-admission-rules").data["data"]["stages"], [])


class AdmissionRulesRefusalTests(_AdmissionFixture):
    """Each refusal is a 400 keyed on its field, in a sentence, and writes nothing."""

    def assert_refused(self, stages, documents=(), *, field="stages", sentence=None):
        before = list(AdmissionStage.all_objects.filter(tenant=self.tenant)
                      .values_list("pk", "name", "position"))
        detail = self.refusal(self.put_rules(stages, documents))
        self.assertIn(field, detail)
        if sentence is not None:
            self.assertEqual(detail[field], [sentence])
        self.assertEqual(
            list(AdmissionStage.all_objects.filter(tenant=self.tenant)
                 .values_list("pk", "name", "position")),
            before,
        )
        return detail

    def test_a_name_listed_twice_ignoring_case(self):
        self.assert_refused(
            [{"name": "Interview"}, {"name": "interview"}],
            sentence="'interview' is listed twice.",
        )

    def test_an_empty_name(self):
        self.assert_refused(
            [{"name": "Interview"}, {"name": "   "}], sentence="Stage 2 needs a name.",
        )

    def test_a_name_longer_than_forty_characters(self):
        self.assert_refused(
            [{"name": "x" * 41}], sentence=f"'{'x' * 41}' is longer than 40 characters.",
        )

    def test_more_than_twelve_stages(self):
        self.assert_refused(
            [{"name": f"Stage {i}"} for i in range(13)],
            sentence="A school can name up to 12 admission stages.",
        )

    def test_offer_days_outside_one_to_a_year(self):
        for days in (0, 366):
            with self.subTest(days=days):
                self.assert_refused(
                    [{"name": "Offer", "is_offer": True, "offer_valid_days": days}],
                    sentence="An offer at 'Offer' can stay open for 1 to 365 days.",
                )

    def test_offer_days_on_a_stage_that_is_not_an_offer(self):
        self.assert_refused(
            [{"name": "Interview", "is_offer": False, "offer_valid_days": 5}],
            sentence=(
                "'Interview' is not an offer stage, so it has no number of days "
                "to accept."
            ),
        )

    def test_removing_a_stage_that_still_holds_applicants(self):
        stages = self.standard_stages()
        for i in range(3):
            self.move(self.applicant(first=f"Child{i}"), stages["Interview"])
        keep = [
            {"id": stages["Entrance exam"], "name": "Entrance exam"},
            {"id": stages["Offer"], "name": "Offer", "is_offer": True, "offer_valid_days": 14},
            {"id": stages["Accepted"], "name": "Accepted"},
        ]
        self.assert_refused(
            keep,
            sentence=(
                "3 applicants are at Interview. Move them to another stage "
                "before removing it."
            ),
        )

    def test_removing_a_stage_holding_one_applicant_says_so_in_the_singular(self):
        stages = self.standard_stages()
        self.move(self.applicant(), stages["Accepted"])
        self.assert_refused(
            [], sentence=(
                "1 applicant is at Accepted. Move them to another stage before "
                "removing it."
            ),
        )

    def test_a_stage_whose_applicants_have_all_been_confirmed_can_go(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        self.move(tunde, stages["Interview"])
        self.assertEqual(self.post(self.admin, "student-confirm", {}, pk=tunde.pk).status_code, 200)
        self.set_rules([])

    def test_an_unknown_document_type(self):
        self.assert_refused(
            [], ["BIRTH_CERTIFICATE", "PASSPORT"], field="required_documents_to_confirm",
            sentence="'PASSPORT' is not a document this school can ask for.",
        )

    def test_a_stage_id_listed_twice(self):
        stages = self.standard_stages()
        self.assert_refused(
            [{"id": stages["Offer"], "name": "Offer"}, {"id": stages["Offer"], "name": "Offer 2"}],
            sentence=f"Stage {stages['Offer']} is listed twice.",
        )


# ── moving an applicant ─────────────────────────────────────────────────────

class StageMoveTests(_AdmissionFixture):

    def test_entering_an_offer_stage_dates_the_offer_from_the_schools_today(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        with school_day(2026, 3, 10):
            response = self.move(tunde, stages["Offer"], reason="Passed the interview.")
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["data"]
        self.assertEqual(row["admission_stage"], stages["Offer"])
        self.assertEqual(row["admission_stage_name"], "Offer")
        self.assertEqual(row["stage_entered_on"], "2026-03-10")
        self.assertEqual(row["offer_expires_on"], "2026-03-24")
        self.assertFalse(row["offer_expired"])
        # The directory row shape, nothing more.
        listed = self.get(self.admin, "student-list").data["data"]
        self.assertEqual(set(row), set(listed["results"][0]) if isinstance(listed, dict)
                         else set(listed[0]))

    def test_leaving_an_offer_stage_clears_the_offer(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        with school_day(2026, 3, 10):
            self.move(tunde, stages["Offer"])
        with school_day(2026, 3, 12):
            row = self.move(tunde, stages["Accepted"]).data["data"]
        self.assertIsNone(row["offer_expires_on"])
        self.assertEqual(row["stage_entered_on"], "2026-03-12")
        with school_day(2026, 3, 13):
            row = self.move(tunde, None).data["data"]
        self.assertIsNone(row["admission_stage"])
        self.assertEqual(row["admission_stage_name"], "")
        self.assertIsNone(row["stage_entered_on"])

    def test_a_move_may_name_its_own_last_day(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        with school_day(2026, 3, 10):
            row = self.move(tunde, stages["Offer"], offer_expires_on="2026-04-30").data["data"]
            self.assertEqual(row["offer_expires_on"], "2026-04-30")
            past = self.refusal(self.move(tunde, stages["Offer"], offer_expires_on="2026-03-09"))
            self.assertEqual(past["offer_expires_on"], ["The last day to accept cannot be in the past."])
            other = self.refusal(self.move(tunde, stages["Interview"], offer_expires_on="2026-05-01"))
            self.assertEqual(other["offer_expires_on"], ["Only an offer stage has a last day to accept."])
        tunde.refresh_from_db()
        self.assertEqual(tunde.offer_expires_on, dt.date(2026, 4, 30))
        self.assertEqual(tunde.admission_stage_id, stages["Offer"])

    def test_an_offer_expires_the_day_after_its_last_day_and_is_flagged_not_rejected(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        with school_day(2026, 3, 10):
            self.move(tunde, stages["Offer"])

        def read():
            detail = self.get(self.admin, "student-detail", pk=tunde.pk).data["data"]
            listed = self.get(self.admin, "student-list", {"stage": stages["Offer"]}).data["data"]
            rows = listed["results"] if isinstance(listed, dict) else listed
            return detail["offer_expired"], rows[0]["offer_expired"]

        with school_day(2026, 3, 24):
            self.assertEqual(read(), (False, False))
        with school_day(2026, 3, 25):
            self.assertEqual(read(), (True, True))
        tunde.refresh_from_db()
        self.assertEqual(tunde.status, StudentStatus.APPLICANT)

    def test_an_expired_offer_is_extended_by_moving_to_the_same_stage(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        with school_day(2026, 3, 10):
            self.move(tunde, stages["Offer"])
        with school_day(2026, 3, 30):
            row = self.move(tunde, stages["Offer"], offer_expires_on="2026-04-06").data["data"]
        self.assertEqual(row["stage_entered_on"], "2026-03-10")
        self.assertEqual(row["offer_expires_on"], "2026-04-06")
        self.assertFalse(row["offer_expired"])

    def test_an_offer_stage_with_no_days_has_no_last_day_unless_the_move_names_one(self):
        data = self.set_rules([{"name": "Conditional offer", "is_offer": True}])
        stage = data["stages"][0]
        self.assertEqual((stage["is_offer"], stage["offer_valid_days"]), (True, None))
        tunde = self.applicant()
        with school_day(2026, 3, 10):
            row = self.move(tunde, stage["id"]).data["data"]
            self.assertIsNone(row["offer_expires_on"])
            row = self.move(tunde, stage["id"], offer_expires_on="2026-03-31").data["data"]
            self.assertEqual(row["offer_expires_on"], "2026-03-31")

    def test_a_move_that_changes_nothing_writes_nothing(self):
        from vs_audit.models import AuditEvent

        stages = self.standard_stages()
        tunde = self.applicant()
        self.move(tunde, stages["Interview"])
        before = AuditEvent.objects.filter(entity_type="Student", entity_id=str(tunde.pk)).count()
        self.assertEqual(self.move(tunde, stages["Interview"]).status_code, 200)
        self.assertEqual(
            AuditEvent.objects.filter(entity_type="Student", entity_id=str(tunde.pk)).count(),
            before,
        )

    def test_the_move_is_audited_with_where_from_and_where_to(self):
        from vs_audit.models import AuditEvent

        stages = self.standard_stages()
        tunde = self.applicant()
        with school_day(2026, 3, 10):
            self.move(tunde, stages["Interview"])
            self.move(tunde, stages["Offer"], reason="Strong interview.")
        event = AuditEvent.objects.filter(
            entity_type="Student", entity_id=str(tunde.pk), action_type="UPDATE",
        ).order_by("-event_at", "-id").first()
        self.assertEqual(event.metadata["from"], {"id": stages["Interview"], "name": "Interview"})
        self.assertEqual(event.metadata["to"], {"id": stages["Offer"], "name": "Offer"})
        self.assertEqual(event.metadata["offer_expires_on"], "2026-03-24")
        self.assertIn("Strong interview.", event.summary)

    def test_a_student_who_is_not_an_applicant_is_refused(self):
        stages = self.standard_stages()
        chiamaka = self.student()
        response = self.move(chiamaka, stages["Interview"])
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "NOT_AN_APPLICANT")
        self.assertEqual(response.data["error"]["detail"], {"status": "ACTIVE"})
        self.assertEqual(
            response.data["message"],
            "Chiamaka Nwosu is active, so there is no admission stage to move. "
            "Stages apply to applicants only.",
        )

    def test_confirming_or_rejecting_keeps_the_stage_as_history(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        kemi = self.applicant(first="Kemi")
        with school_day(2026, 3, 10):
            self.move(tunde, stages["Offer"])
            self.move(kemi, stages["Offer"])
        with school_day(2026, 4, 1):
            self.assertEqual(self.post(self.admin, "student-confirm", {}, pk=tunde.pk).status_code, 200)
            self.assertEqual(self.post(
                self.admin, "student-reject", {"reason": "Declined the offer."}, pk=kemi.pk,
            ).status_code, 200)
            for child in (tunde, kemi):
                row = self.get(self.admin, "student-detail", pk=child.pk).data["data"]
                self.assertEqual(row["admission_stage_name"], "Offer")
                self.assertEqual(row["offer_expires_on"], "2026-03-24")
                self.assertFalse(row["offer_expired"])

    def test_a_past_view_reads_the_stage_then_and_judges_the_offer_on_that_day(self):
        """Tunde's offer ran to 24 March; the profile read as at the 30th says expired."""
        from zoneinfo import ZoneInfo

        from vs_config.clock import DEFAULT_TIME_ZONE

        def recorded(month, day):
            moment = dt.datetime(2026, month, day, 10, tzinfo=ZoneInfo(DEFAULT_TIME_ZONE))
            return mock.patch("vs_history.recorder.timezone.now", return_value=moment)

        stages = self.standard_stages()
        with recorded(3, 1):
            tunde = self.applicant()
        with recorded(3, 10), school_day(2026, 3, 10):
            self.move(tunde, stages["Offer"])

        def as_at(day):
            return self.get(
                self.admin, "student-detail", {"as_at": day}, pk=tunde.pk,
            ).data["data"]

        with school_day(2026, 3, 20):
            before = as_at("2026-03-05")
            self.assertIsNone(before["admission_stage"])
            self.assertFalse(before["offer_expired"])
            later = as_at("2026-03-30")
            self.assertEqual(later["admission_stage_name"], "Offer")
            self.assertTrue(later["offer_expired"])
            self.assertFalse(
                self.get(self.admin, "student-detail", pk=tunde.pk).data["data"]["offer_expired"],
            )

        # A stage removed since reads with no name rather than failing.
        with recorded(3, 21), school_day(2026, 3, 21):
            self.post(self.admin, "student-confirm", {}, pk=tunde.pk)
        self.set_rules([])
        self.assertEqual(as_at("2026-03-12")["admission_stage_name"], "")

    def test_an_empty_body_is_refused_rather_than_read_as_no_stage(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        self.move(tunde, stages["Interview"])
        detail = self.refusal(self.post(self.admin, "student-stage", {}, pk=tunde.pk))
        self.assertIn("stage", detail)
        tunde.refresh_from_db()
        self.assertEqual(tunde.admission_stage_id, stages["Interview"])


# ── the directory ───────────────────────────────────────────────────────────

class StageFilterTests(_AdmissionFixture):

    def rows(self, params):
        data = self.get(self.admin, "student-list", params).data["data"]
        rows = data["results"] if isinstance(data, dict) else data
        return {row["first_name"] for row in rows}

    def test_the_stage_filter_lists_applicants_at_that_stage_only(self):
        stages = self.standard_stages()
        self.move(self.applicant(first="Tunde"), stages["Interview"])
        self.move(self.applicant(first="Kemi", branch=self.ikeja), stages["Interview"])
        self.move(self.applicant(first="Bayo"), stages["Offer"])
        self.applicant(first="Ngozi")
        confirmed = self.applicant(first="Emeka")
        self.move(confirmed, stages["Interview"])
        self.post(self.admin, "student-confirm", {}, pk=confirmed.pk)

        self.assertEqual(self.rows({"stage": stages["Interview"]}), {"Tunde", "Kemi"})
        self.assertEqual(self.rows({"stage": stages["Offer"]}), {"Bayo"})
        self.assertEqual(self.rows({"stage": "none"}), {"Ngozi"})
        self.assertEqual(
            self.rows({"stage": stages["Interview"], "branch": self.ikeja.pk}), {"Kemi"},
        )

    def test_a_malformed_stage_is_refused(self):
        detail = self.refusal(self.get(self.admin, "student-list", {"stage": "interview"}))
        self.assertEqual(detail["stage"], ["No such admission stage at this school."])

    def test_the_list_costs_the_same_however_many_applicants_have_stages(self):
        stages = self.standard_stages()
        with school_day(2026, 3, 10):
            for i in range(2):
                self.move(self.applicant(first=f"First{i}"), stages["Offer"])
            with CaptureQueriesContext(connection) as first:
                self.get(self.admin, "student-list")
            for i in range(6):
                name = ("Interview", "Offer", "Accepted")[i % 3]
                self.move(self.applicant(first=f"More{i}"), stages[name])
            with CaptureQueriesContext(connection) as second:
                self.get(self.admin, "student-list")
        self.assertEqual(len(second), len(first))


# ── confirming ──────────────────────────────────────────────────────────────

class ConfirmDocumentsTests(_AdmissionFixture):

    def test_a_missing_document_refuses_confirmation_by_name(self):
        self.set_rules([], ["BIRTH_CERTIFICATE", "TRANSFER_CERTIFICATE"])
        tunde = self.applicant()
        self.attach(tunde, DocumentType.PASSPORT_PHOTO)
        response = self.post(self.admin, "student-confirm", {}, pk=tunde.pk)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "DOCUMENTS_MISSING")
        self.assertEqual(
            response.data["message"],
            "Tunde Bello cannot be confirmed until the birth certificate and "
            "transfer certificate are on their record.",
        )
        self.assertEqual(response.data["error"]["detail"], {"missing": [
            {"value": "BIRTH_CERTIFICATE", "label": "Birth certificate"},
            {"value": "TRANSFER_CERTIFICATE", "label": "Transfer certificate"},
        ]})
        tunde.refresh_from_db()
        self.assertEqual(tunde.status, StudentStatus.APPLICANT)

    def test_confirmation_passes_once_the_documents_are_there(self):
        self.set_rules([], ["BIRTH_CERTIFICATE"])
        tunde = self.applicant()
        refused = self.post(self.admin, "student-confirm", {}, pk=tunde.pk)
        self.assertEqual(refused.data["message"],
                         "Tunde Bello cannot be confirmed until the birth certificate "
                         "is on their record.")
        self.attach(tunde)
        confirmed = self.post(self.admin, "student-confirm", {}, pk=tunde.pk)
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["data"]["status"], StudentStatus.ENROLLED)

    def test_the_status_routes_wait_for_the_documents_too(self):
        self.set_rules([], ["BIRTH_CERTIFICATE"])
        tunde = self.applicant()
        kemi = self.applicant(first="Kemi")
        one = self.post(
            self.admin, "student-status",
            {"to_status": "ENROLLED", "reason": "Offer accepted."}, pk=tunde.pk,
        )
        self.assertEqual(one.data["error"]["code"], "DOCUMENTS_MISSING")
        bulk = self.post(self.admin, "student-bulk-status", {
            "student_ids": [kemi.pk], "to_status": "ENROLLED", "reason": "Offer accepted.",
        })
        self.assertEqual(bulk.data["data"]["results"][0]["code"], "DOCUMENTS_MISSING")

    def test_a_refusal_issues_no_admission_number(self):
        self.put(self.admin, "student-admission-policy", {
            "required": False, "pattern": r"BFS/\d{4}", "hint": "", "auto_issue": True,
        })
        self.set_rules([], ["BIRTH_CERTIFICATE"])
        tunde = self.applicant()
        self.post(self.admin, "student-confirm", {}, pk=tunde.pk)
        tunde.refresh_from_db()
        self.assertEqual(tunde.student_number, "")

    def test_the_stage_does_not_gate_confirmation(self):
        stages = self.standard_stages()
        tunde = self.applicant()
        self.move(tunde, stages["Entrance exam"])
        self.assertEqual(self.post(self.admin, "student-confirm", {}, pk=tunde.pk).status_code, 200)

    def test_enrolling_directly_is_held_to_the_same_documents(self):
        """Skipping the applicant stage does not skip its documents.

        The enrolment's own refusals and its multipart form are covered in
        ``test_enrolment_documents``.
        """
        self.set_rules([], ["BIRTH_CERTIFICATE"])
        response = self.post(self.admin, "student-list", self.enrolment_body())
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "DOCUMENTS_MISSING")


# ── a school with no stages ─────────────────────────────────────────────────

class NoStagesTests(_AdmissionFixture):
    """A school that has named nothing admits on the spot, exactly as before."""

    def test_an_applicant_is_saved_listed_and_confirmed_as_before(self):
        body = self.enrolment_body(as_applicant=True, applied_for=self.jss1.pk)
        saved = self.post(self.admin, "student-list", body)
        self.assertEqual(saved.status_code, 201, saved.data)
        row = saved.data["data"]
        self.assertIsNone(row["admission_stage"])
        self.assertEqual(row["admission_stage_name"], "")
        self.assertIsNone(row["stage_entered_on"])
        self.assertIsNone(row["offer_expires_on"])
        self.assertFalse(row["offer_expired"])
        listed = self.get(self.admin, "student-list", {"stage": "none"}).data["data"]
        rows = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual([r["id"] for r in rows], [row["id"]])
        confirmed = self.post(self.admin, "student-confirm", {}, pk=row["id"])
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

    def test_a_single_branch_school_with_no_stages_confirms_as_before(self):
        amaka = self.applicant(tenant=self.solo.tenant, branch=self.solo_branch, first="Amaka")
        rules = self.get(self.solo_admin, "student-admission-rules").data["data"]
        self.assertEqual(rules["stages"], [])
        listed = self.get(self.solo_admin, "student-list", {"stage": "none"}).data["data"]
        rows = listed["results"] if isinstance(listed, dict) else listed
        self.assertEqual([r["id"] for r in rows], [amaka.pk])
        self.assertNotIn("branch", rows[0])
        confirmed = self.post(self.solo_admin, "student-confirm", {}, pk=amaka.pk)
        self.assertEqual(confirmed.status_code, 200, confirmed.data)


# ── the catalogue ───────────────────────────────────────────────────────────

class AdmissionCatalogueTests(_AdmissionFixture):

    def test_the_key_exists_with_todays_default_and_a_school_scope(self):
        from vs_config.models import ConfigurationDefinition

        row = ConfigurationDefinition.objects.get(key="applicants.documents.required_to_confirm")
        self.assertEqual(row.default_value, [])
        self.assertEqual(sorted(row.allowed_scopes), ["platform", "school"])

    def test_a_bad_stored_value_costs_the_school_its_rule_not_a_confirmation(self):
        from vs_config.models import ConfigurationDefinition, ConfigurationValue

        from ..services.admission import confirm_documents

        ConfigurationValue.all_objects.create(
            definition=ConfigurationDefinition.objects.get(
                key="applicants.documents.required_to_confirm",
            ),
            scope_key="platform", value=["PASSPORT", "BIRTH_CERTIFICATE", 7],
        )
        self.assertEqual(confirm_documents(self.tenant), ("BIRTH_CERTIFICATE",))


# ── the migration ───────────────────────────────────────────────────────────

@tag("slow")
class AdmissionStagesMigrationTests(RewoundSchemaTestCase):
    """0008 adds the stages and the setting without touching a student, and reverses."""

    APP = "vs_students"
    BEFORE = "0007_guardian_settings"
    AFTER = "0008_admission_stages"
    KEY = "applicants.documents.required_to_confirm"

    def test_forward_then_back(self):
        school = make_school(slug="migration-adm", name="Migration Academy")
        Definition = self.historical.get_model("vs_config", "ConfigurationDefinition")
        self.assertFalse(Definition.objects.filter(key=self.KEY).exists())

        self.migrate_to(self.AFTER)
        after = self.historical_apps(self.AFTER)
        self.assertTrue(
            after.get_model("vs_config", "ConfigurationDefinition")
            .objects.filter(key=self.KEY).exists(),
        )
        after.get_model("vs_students", "AdmissionStage").objects.create(
            tenant_id=school.tenant_id, name="Interview", position=1,
        )

        self.migrate_to(self.BEFORE)
        before = self.historical_apps(self.BEFORE)
        self.assertFalse(
            before.get_model("vs_config", "ConfigurationDefinition")
            .objects.filter(key=self.KEY).exists(),
        )
        with connection.cursor() as cursor:
            tables = connection.introspection.table_names(cursor)
        self.assertNotIn("vs_students_admissionstage", tables)
