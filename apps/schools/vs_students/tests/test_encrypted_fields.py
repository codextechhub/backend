"""The encrypted columns, proved on a child's medical fields.

``core.encryption`` and ``core.fields.EncryptedTextField`` are shared; a
child's allergies are the column the stakes are clearest on, so the proof is
written against them: the table holds no plain text, the history holds none,
the audit trail holds none, and the screens read the words back.
"""
from __future__ import annotations

from io import StringIO

from cryptography.fernet import Fernet
from django.core.exceptions import FieldError
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, override_settings

from core import encryption
from core.encryption import DecryptionError, LAST_FOUR, WITHHELD, WITHHOLD, withhold
from schools.vs_students.models import Student
from vs_audit.models import AuditEvent
from vs_audit.services import emit_audit_event
from vs_history.models import RecordVersion
from vs_history.registry import rebuild, spec_for

from .base import StudentsFixture

NEW_KEY = Fernet.generate_key().decode()


def _stored(student, column="allergies"):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM vs_students_student WHERE id = %s", [student.pk])
        return cursor.fetchone()[0]


class EncryptionTests(SimpleTestCase):
    def test_a_value_round_trips_and_a_blank_stays_blank(self):
        stored = encryption.encrypt("Peanuts")
        self.assertTrue(stored.startswith("enc1:"))
        self.assertNotIn("Peanuts", stored)
        self.assertNotEqual(stored, encryption.encrypt("Peanuts"))
        self.assertEqual(encryption.decrypt(stored), "Peanuts")
        self.assertEqual(encryption.encrypt(""), "")
        self.assertIsNone(encryption.encrypt(None))

    def test_a_value_written_before_encryption_reads_as_it_was(self):
        self.assertEqual(encryption.decrypt("Peanuts"), "Peanuts")

    def test_a_rotated_key_still_opens_what_the_old_one_sealed(self):
        old = encryption.encrypt("Peanuts")
        with override_settings(FIELD_ENCRYPTION_KEYS=[NEW_KEY, encryption.DEV_KEY]):
            self.assertEqual(encryption.decrypt(old), "Peanuts")
        with override_settings(FIELD_ENCRYPTION_KEYS=[NEW_KEY]):
            with self.assertRaises(DecryptionError):
                encryption.decrypt(old)

    def test_audit_payloads_withhold_medical_values_and_shorten_account_numbers(self):
        self.assertEqual(encryption.SEALED_NAMES["allergies"], WITHHOLD)
        self.assertEqual(encryption.SEALED_NAMES["account_number"], LAST_FOUR)
        self.assertEqual(encryption.SEALED_NAMES["pension_pin"], LAST_FOUR)
        payload = {
            "allergies": {"from": "", "to": "Peanuts"},
            "account_number": {"from": "0123456789", "to": "9876543210"},
            "first_name": "Chiamaka",
            "nested": [{"conditions": "Asthma"}],
        }
        self.assertEqual(withhold(payload), {
            "allergies": {"from": "", "to": WITHHELD},
            "account_number": {"from": "****6789", "to": "****3210"},
            "first_name": "Chiamaka",
            "nested": [{"conditions": WITHHELD}],
        })


class StudentMedicalFieldTests(StudentsFixture):
    def setUp(self):
        super().setUp()
        self.pupil = self.student(allergies="Peanuts", conditions="Asthma", blood_group="O+")

    def test_the_table_holds_no_plain_text_and_the_record_reads_it_back(self):
        for column in ("allergies", "conditions", "blood_group"):
            with self.subTest(column=column):
                self.assertTrue(_stored(self.pupil, column).startswith("enc1:"))
        fresh = Student.all_objects.get(pk=self.pupil.pk)
        self.assertEqual((fresh.allergies, fresh.conditions, fresh.blood_group),
                         ("Peanuts", "Asthma", "O+"))
        self.assertEqual(
            Student.all_objects.filter(pk=self.pupil.pk).values_list("allergies", flat=True)[0],
            "Peanuts",
        )

    def test_only_blank_and_null_can_be_matched_in_the_database(self):
        self.assertTrue(Student.all_objects.filter(pk=self.pupil.pk).exclude(allergies="").exists())
        self.assertFalse(Student.all_objects.filter(pk=self.pupil.pk, allergies__isnull=True).exists())
        with self.assertRaises(FieldError):
            list(Student.all_objects.filter(allergies="Peanuts"))
        with self.assertRaises(FieldError):
            list(Student.all_objects.filter(allergies__icontains="nut"))

    def test_the_history_keeps_the_encrypted_form_and_records_real_changes_only(self):
        spec = spec_for(Student)
        versions = RecordVersion.objects.filter(
            record_type=spec.record_type, record_id=str(self.pupil.pk),
        )
        before = versions.count()

        self.pupil.save()  # Same words, a fresh ciphertext: no change happened.
        self.assertEqual(versions.count(), before)

        self.pupil.allergies = "Peanuts, shellfish"
        self.pupil.save()
        latest = versions.order_by("-id").first()
        self.assertEqual(versions.count(), before + 1)
        self.assertEqual(latest.changed, ["allergies"])
        self.assertTrue(latest.data["allergies"].startswith("enc1:"))
        self.assertEqual(rebuild(spec, self.pupil.pk, latest.data).allergies, "Peanuts, shellfish")

    def test_the_audit_trail_withholds_the_value(self):
        emit_audit_event(
            module_key="STUDENT", action_type="UPDATE", entity_type="Student",
            entity_id=str(self.pupil.pk), tenant=self.tenant,
            before_data={"allergies": "Peanuts"},
            diff_data={"allergies": {"from": "Peanuts", "to": "Shellfish"}},
        )
        event = AuditEvent.objects.filter(entity_id=str(self.pupil.pk)).latest("pk")
        self.assertEqual(event.before_data["allergies"], WITHHELD)
        self.assertEqual(event.diff_data["allergies"], {"from": WITHHELD, "to": WITHHELD})

    def test_rotation_reseals_rows_and_history_with_the_new_key(self):
        with override_settings(FIELD_ENCRYPTION_KEYS=[NEW_KEY, encryption.DEV_KEY]):
            call_command("reencrypt_fields", stdout=StringIO())
        with override_settings(FIELD_ENCRYPTION_KEYS=[NEW_KEY]):
            self.assertEqual(Student.all_objects.get(pk=self.pupil.pk).allergies, "Peanuts")
            spec = spec_for(Student)
            for version in RecordVersion.objects.filter(
                record_type=spec.record_type, record_id=str(self.pupil.pk),
            ):
                encryption.decrypt(version.data["allergies"])  # Opens with the new key alone.
