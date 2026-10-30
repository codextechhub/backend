"""Erasing pupils, parents and staff 6 years after they leave.

Bright Star's Tunde left in 2019 and his classmate Amaka in 2022; Mrs Adeyemi
resigned in 2019. On the night of this run Tunde and Mrs Adeyemi are past 6
years and Amaka is not. Erasure keeps their names and the records the school
must keep, and takes everything else about them.
"""
from __future__ import annotations

import datetime as dt
import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from core import pruning
from core.models import StoredFile
from schools.vs_staff.models import (
    LeaveRequest, StaffDocument, StaffEmploymentEvent, StaffProfile, StaffQualification,
)
from schools.vs_students.constants import Gender, Relationship, StudentStatus
from schools.vs_students.models import (
    ClassEnrolment, Guardian, Student, StudentDocument, StudentGuardian, StudentStatusLog,
)
from vs_history.models import RecordVersion
from vs_history.registry import spec_for
from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin, make_vision_user
from vs_user.models import PlatformStaffProfile, User

NOW = timezone.now()
TODAY = NOW.date()
SEVEN_YEARS_AGO = TODAY - dt.timedelta(days=7 * 365)
FIVE_YEARS_AGO = TODAY - dt.timedelta(days=5 * 365)


def versions_of(row):
    return RecordVersion.objects.filter(
        record_type=spec_for(type(row)).record_type, record_id=str(row.pk),
    )


def history_text(row):
    return json.dumps([v.data for v in versions_of(row)])


class ErasureFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="bright-star", name="Bright Star")
        cls.tenant = cls.school.tenant
        cls.ikeja = make_branch(cls.school, name="Ikeja", is_main=True)
        cls.recorder = make_school_admin(None, email="registrar@brightstar.test", tenant=cls.tenant)

    def run_erasure(self, dry_run=False):
        periods = pruning.run(now=NOW, dry_run=dry_run)["periods"]
        for key in ("former_pupils_and_parents", "former_staff", "former_codex_staff"):
            self.assertNotIn("stopped", periods[key], periods[key])
        return periods

    def account(self, email, **fields):
        return make_school_admin(None, email=email, tenant=self.tenant, phone="08030000000", **fields)

    def pupil(self, first, number, *, left_on=None, status=StudentStatus.GRADUATED, user=None):
        pupil = Student.all_objects.create(
            tenant=self.tenant, branch=self.ikeja, first_name=first, last_name="Okafor",
            student_number=number, date_of_birth=dt.date(2008, 4, 18), gender=Gender.MALE,
            status=StudentStatus.ACTIVE, enrolment_date=dt.date(2014, 9, 8),
            address="12 Allen Avenue", phone="08031112222", email=f"{first.lower()}@family.test",
            allergies="Peanuts", emergency_contact_name="Aunt Bisi", user=user,
        )
        if left_on:
            StudentStatusLog.all_objects.create(
                tenant=self.tenant, student=pupil, from_status=StudentStatus.ACTIVE,
                to_status=status, reason="Moving to Abuja", destination_school="Capital College",
                effective_date=left_on,
            )
            Student.all_objects.filter(pk=pupil.pk).update(status=status)
            pupil.refresh_from_db()
        return pupil

    def parent_of(self, *pupils, user=None):
        parent = Guardian.all_objects.create(
            tenant=self.tenant, first_name="Ngozi", last_name="Okafor", phone="08035550101",
            email=f"ngozi{pupils[0].pk}@family.test", occupation="Nurse", address="12 Allen Avenue",
            user=user,
        )
        for pupil in pupils:
            StudentGuardian.all_objects.create(
                tenant=self.tenant, student=pupil, guardian=parent,
                relationship=Relationship.MOTHER, is_primary=True,
            )
        return parent


class PupilErasureTests(ErasureFixture):
    def test_a_pupil_six_years_gone_keeps_his_name_and_loses_the_rest(self):
        from schools.vs_messages.models import Conversation, Message

        login = self.account("tunde@pupils.test", first_name="Tunde", last_name="Okafor")
        tunde = self.pupil("Tunde", "BS-001", left_on=SEVEN_YEARS_AGO, user=login)
        parent = self.parent_of(tunde)
        birth_certificate = StudentDocument.all_objects.create(
            tenant=self.tenant, student=tunde, document_type="BIRTH_CERTIFICATE",
            file=SimpleUploadedFile("birth.pdf", b"%PDF certificate"), original_name="birth.pdf",
        )
        stored_name = birth_certificate.file.name
        chat = Conversation.all_objects.create(
            tenant=self.tenant, kind="FAMILY", pair_key="family:t", student=tunde, guardian=parent,
        )
        Message.all_objects.create(tenant=self.tenant, conversation=chat, body="He is unwell")

        self.run_erasure()
        tunde.refresh_from_db()

        self.assertIsNotNone(tunde.erased_at)
        self.assertEqual((tunde.first_name, tunde.last_name, tunde.student_number),
                         ("Tunde", "Okafor", "BS-001"))
        self.assertEqual(tunde.date_of_birth, dt.date(2008, 1, 1))
        for field in ("address", "phone", "email", "allergies", "emergency_contact_name", "gender"):
            self.assertEqual(getattr(tunde, field), "", field)
        self.assertFalse(StudentDocument.all_objects.filter(pk=birth_certificate.pk).exists())
        self.assertIsNotNone(StoredFile.objects.get(name=stored_name).revoked_at)
        self.assertFalse(Conversation.all_objects.filter(pk=chat.pk).exists())
        log = StudentStatusLog.all_objects.get(student=tunde)
        self.assertEqual((log.reason, log.destination_school), ("", ""))
        self.assertEqual(log.effective_date, SEVEN_YEARS_AGO)

        history = history_text(tunde)
        self.assertNotIn("Allen Avenue", history)
        self.assertNotIn("Peanuts", history)
        self.assertNotIn("2008-04-18", history)
        self.assertIn("Tunde", history)

        login.refresh_from_db()
        self.assertFalse(login.is_active)
        self.assertEqual(login.email, f"erased-{login.pk}@erased.invalid")
        self.assertIsNone(login.phone)
        self.assertEqual(login.first_name, "Tunde")

    def test_his_results_and_his_name_on_the_books_stay(self):
        from schools.core.fal.contracts import SOURCE_TYPE_STUDENT
        from vs_finance.models import Customer, LedgerEntity

        from schools.vs_academics.models import AcademicSession, Level, Program, SchoolClass

        tunde = self.pupil("Tunde", "BS-001", left_on=SEVEN_YEARS_AGO)
        year = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2018/2019", start_date=dt.date(2018, 9, 1),
            end_date=dt.date(2019, 7, 31),
        )
        junior = Program.all_objects.create(tenant=self.tenant, name="Junior", code="JNR")
        level = Level.all_objects.create(
            tenant=self.tenant, session=year, program=junior, branch=self.ikeja,
            name="JSS 3", code="JSS3", order_index=3,
        )
        jss3 = SchoolClass.all_objects.create(
            tenant=self.tenant, session=year, level=level, name="JSS 3A", code="JSS3A",
            branch=self.ikeja,
        )
        enrolment = ClassEnrolment.all_objects.create(
            tenant=self.tenant, student=tunde, school_class=jss3, session=year,
            is_active=False, effective_date=dt.date(2018, 9, 1),
        )
        books = LedgerEntity.objects.create(name="Bright Star Books", code="BSB", tenant=self.tenant)
        account = Customer.objects.create(
            entity=books, code="STU-001", name="Tunde Okafor", source_type=SOURCE_TYPE_STUDENT,
            source_id=str(tunde.pk), billing_email="ngozi@family.test",
            billing_phone="08035550101", billing_address="12 Allen Avenue",
        )

        self.run_erasure()

        self.assertTrue(ClassEnrolment.all_objects.filter(pk=enrolment.pk).exists())
        account.refresh_from_db()
        self.assertEqual(account.name, "Tunde Okafor")
        self.assertEqual(
            (account.billing_email, account.billing_phone, account.billing_address), ("", "", ""),
        )

    def test_a_pupil_who_left_five_years_ago_and_one_still_here_are_kept(self):
        amaka = self.pupil("Amaka", "BS-002", left_on=FIVE_YEARS_AGO)
        chidi = self.pupil("Chidi", "BS-003")
        Student.all_objects.filter(pk=chidi.pk).update(updated_at=NOW - dt.timedelta(days=8 * 365))

        self.run_erasure()

        for pupil in (amaka, chidi):
            pupil.refresh_from_db()
            self.assertIsNone(pupil.erased_at)
            self.assertEqual(pupil.address, "12 Allen Avenue")

    def test_a_dry_run_counts_and_changes_nothing_and_a_second_run_finds_nobody(self):
        tunde = self.pupil("Tunde", "BS-001", left_on=SEVEN_YEARS_AGO)

        dry = self.run_erasure(dry_run=True)["former_pupils_and_parents"]["rows"]
        self.assertEqual(dry["pupils"], 1)
        tunde.refresh_from_db()
        self.assertIsNone(tunde.erased_at)

        self.run_erasure()
        again = self.run_erasure()["former_pupils_and_parents"]["rows"]
        self.assertEqual(again["pupils"], {"erased": 0, "failed": 0})

    def test_one_pupil_failing_does_not_hold_back_the_next(self):
        tunde = self.pupil("Tunde", "BS-001", left_on=SEVEN_YEARS_AGO)
        bola = self.pupil("Bola", "BS-004", left_on=SEVEN_YEARS_AGO)
        from schools.vs_students import erasure

        real = erasure.erase_pupil

        def flaky(pupil):
            if pupil.pk == tunde.pk:
                raise RuntimeError("storage down")
            real(pupil)

        with mock.patch.object(erasure, "erase_pupil", flaky):
            rows = self.run_erasure()["former_pupils_and_parents"]["rows"]

        self.assertEqual(rows["pupils"], {"erased": 1, "failed": 1})
        tunde.refresh_from_db()
        bola.refresh_from_db()
        self.assertIsNone(tunde.erased_at)
        self.assertIsNotNone(bola.erased_at)


class ParentErasureTests(ErasureFixture):
    def test_a_parent_goes_once_her_last_child_has_been_erased(self):
        login = self.account("ngozi@family.test", first_name="Ngozi", last_name="Okafor")
        tunde = self.pupil("Tunde", "BS-001", left_on=SEVEN_YEARS_AGO)
        parent = self.parent_of(tunde, user=login)

        self.run_erasure()
        parent.refresh_from_db()
        login.refresh_from_db()

        self.assertIsNotNone(parent.erased_at)
        self.assertEqual((parent.first_name, parent.last_name, parent.full_name),
                         ("Former", "parent", "Former parent"))
        for field in ("phone", "email", "occupation", "address"):
            self.assertEqual(getattr(parent, field), "", field)
        self.assertNotIn("Ngozi", history_text(parent))
        self.assertEqual((login.first_name, login.last_name), ("Former", "parent"))
        self.assertFalse(login.is_active)

    def test_a_parent_with_a_child_still_at_school_is_kept(self):
        tunde = self.pupil("Tunde", "BS-001", left_on=SEVEN_YEARS_AGO)
        chidi = self.pupil("Chidi", "BS-003")
        parent = self.parent_of(tunde, chidi)

        self.run_erasure()
        parent.refresh_from_db()

        self.assertIsNone(parent.erased_at)
        self.assertEqual(parent.first_name, "Ngozi")


class StaffErasureTests(ErasureFixture):
    def staff(self, email, *, left_on, user=None):
        user = user or self.account(email, first_name="Funke", last_name="Adeyemi", gender="FEMALE")
        staff = StaffProfile.all_objects.create(
            tenant=self.tenant, user=user, branch=self.ikeja, staff_number="ST-01",
            job_title="Maths teacher", employment_status="RESIGNED", exit_date=left_on,
            date_of_birth=dt.date(1985, 2, 3),
        )
        return staff

    def test_a_teacher_six_years_gone_keeps_her_name_and_service_and_loses_the_rest(self):
        from vs_finance.models import LedgerEntity, StaffBankAccount

        adeyemi = self.staff("funke@brightstar.test", left_on=SEVEN_YEARS_AGO)
        StaffQualification.all_objects.create(
            tenant=self.tenant, staff=adeyemi, qualification="B.Sc. Mathematics",
            institution="UNILAG", year_obtained=2007,
        )
        StaffDocument.all_objects.create(
            tenant=self.tenant, staff=adeyemi, document_type="CV", title="CV",
            file=SimpleUploadedFile("cv.pdf", b"%PDF cv"),
        )
        StaffEmploymentEvent.all_objects.create(
            tenant=self.tenant, staff=adeyemi, from_status="ACTIVE", to_status="RESIGNED",
            reason="Relocating", note="Spouse posted to Abuja", effective_date=SEVEN_YEARS_AGO,
        )
        leave = LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=adeyemi, leave_type="SICK", start_date=dt.date(2018, 3, 5),
            end_date=dt.date(2018, 3, 9), days=5, note="Malaria", status="APPROVED",
        )
        books = LedgerEntity.objects.create(name="Bright Star Books", code="BSB", tenant=self.tenant)
        bank = StaffBankAccount.objects.create(
            entity=books, employee=adeyemi.user, bank_code="058", bank_name="GTBank",
            account_number="0123456789", resolved_account_name="ADEYEMI FUNKE",
        )

        self.run_erasure()
        adeyemi.refresh_from_db()
        user = User.objects.get(pk=adeyemi.user_id)

        self.assertIsNotNone(adeyemi.erased_at)
        self.assertIsNone(adeyemi.date_of_birth)
        self.assertEqual((adeyemi.staff_number, adeyemi.job_title, adeyemi.exit_date),
                         ("ST-01", "Maths teacher", SEVEN_YEARS_AGO))
        self.assertFalse(StaffQualification.all_objects.filter(staff=adeyemi).exists())
        self.assertFalse(StaffDocument.all_objects.filter(staff=adeyemi).exists())
        event = StaffEmploymentEvent.all_objects.get(staff=adeyemi)
        self.assertEqual((event.reason, event.note), ("", ""))
        leave.refresh_from_db()
        self.assertEqual((leave.note, leave.leave_type, leave.start_date),
                         ("", "SICK", dt.date(2018, 3, 5)))
        self.assertNotIn("Malaria", history_text(leave))
        self.assertNotIn("1985-02-03", history_text(adeyemi))
        bank.refresh_from_db()
        self.assertEqual((bank.account_number, bank.bank_name), ("", ""))
        self.assertEqual((user.first_name, user.last_name), ("Funke", "Adeyemi"))
        self.assertEqual(user.email, f"erased-{user.pk}@erased.invalid")
        self.assertEqual(user.gender, "")
        self.assertFalse(user.is_active)

    def test_a_former_teacher_still_a_parent_here_keeps_her_sign_in(self):
        login = self.account("funke@brightstar.test", first_name="Funke", last_name="Adeyemi")
        adeyemi = self.staff("funke@brightstar.test", left_on=SEVEN_YEARS_AGO, user=login)
        chidi = self.pupil("Chidi", "BS-003")
        self.parent_of(chidi, user=login)

        self.run_erasure()
        adeyemi.refresh_from_db()
        login.refresh_from_db()

        self.assertIsNotNone(adeyemi.erased_at)
        self.assertTrue(login.is_active)
        self.assertEqual(login.email, "funke@brightstar.test")

    def test_a_teacher_who_left_five_years_ago_is_kept(self):
        adeyemi = self.staff("funke@brightstar.test", left_on=FIVE_YEARS_AGO)
        self.run_erasure()
        adeyemi.refresh_from_db()
        self.assertIsNone(adeyemi.erased_at)
        self.assertEqual(adeyemi.date_of_birth, dt.date(1985, 2, 3))


class CodexStaffErasureTests(ErasureFixture):
    def test_codex_staff_six_years_gone_lose_personal_details_and_bank(self):
        user = make_vision_user(email="emeka@codexng.test")
        profile = PlatformStaffProfile.objects.create(
            user=user, nationality="Nigerian", personal_email="emeka@home.test",
            nok_name="Ada", bank_name="GTBank", account_number="0123456789",
            employment_status=PlatformStaffProfile.EmploymentStatus.EXITED,
            date_exited=SEVEN_YEARS_AGO, job_title="Engineer",
        )
        stays = PlatformStaffProfile.objects.create(
            user=make_vision_user(email="ada@codexng.test"), nok_name="Obi",
            employment_status=PlatformStaffProfile.EmploymentStatus.EXITED,
            date_exited=FIVE_YEARS_AGO,
        )

        self.run_erasure()
        profile.refresh_from_db()
        stays.refresh_from_db()
        user.refresh_from_db()

        self.assertIsNotNone(profile.erased_at)
        self.assertEqual(profile.job_title, "Engineer")
        for field in ("nationality", "personal_email", "nok_name", "bank_name", "account_number"):
            self.assertEqual(getattr(profile, field), "", field)
        self.assertNotIn("emeka@home.test", history_text(profile))
        self.assertFalse(user.is_active)
        self.assertTrue(user.email.endswith("@erased.invalid"))
        self.assertIsNone(stays.erased_at)
        self.assertEqual(stays.nok_name, "Obi")
