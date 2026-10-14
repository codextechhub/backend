"""Brightfield and Sunrise with what a score-sheet upload needs.

Extends :class:`~.sheet_base.SheetFixture`: every teacher holds the import key
beside their sheet keys, and the people who prove the upload's guards exist.

* ``eze`` is the main teacher of JSS1 A Mathematics (and of nothing else),
  ``bello`` assists, and both hold the import key, so only the sheet rule
  separates them.
* ``keyless`` leads JSS1 B Mathematics without the import key, so a refusal can
  only be the key.
* ``enterer`` holds ``sheet.enter_any`` and the import key through a role of its
  own, and ``enter_only`` holds ``enter_any`` and no import key.
* Sunrise's teacher leads Basic 3 Mathematics and holds the import key; Sunrise
  has one branch.

A workbook is built with :func:`workbook`, whose first row is the headings, and
posted with :meth:`ImportFixture.upload`. :data:`HEADINGS` are the three
Mathematics columns as the template writes them.
"""
from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from core.test_utils import TenantAPIClient
from schools.vs_staff.constants import TeachingPart
from schools.vs_staff.models import TeachingAssignment
from vs_rbac.models import PermissionScope, TenantRoleTemplate
from vs_rbac.tests.helpers import make_permission, make_role_permission

from ..constants import PERM_SHEET_ENTER_ANY, PERM_SHEET_IMPORT, PERM_SHEET_VIEW
from .base import TEACHER_KEYS, make_key_role
from .sheet_base import SheetFixture

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ADMISSION = "Admission number"
HEADINGS = [ADMISSION, "Student", "1st CA (max 20)", "2nd CA (max 20)", "Examination (max 60)"]


def workbook(rows, headings=HEADINGS, *, sheet_name="Scores") -> bytes:
    """A workbook of *headings* and *rows* as xlsx bytes."""
    book = Workbook()
    sheet = book.active
    sheet.title = sheet_name
    sheet.append(list(headings))
    for row in rows:
        sheet.append(list(row))
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def csv_bytes(rows, headings=HEADINGS) -> bytes:
    """A CSV of *headings* and *rows*."""
    lines = [",".join(str(cell) for cell in headings)]
    lines += [",".join("" if cell is None else str(cell) for cell in row) for row in rows]
    return ("\n".join(lines) + "\n").encode()


class ImportFixture(SheetFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.grant(cls.teacher_role, PERM_SHEET_IMPORT)
        cls.grant(TenantRoleTemplate.objects.get(tenant=cls.solo, key="teacher"), PERM_SHEET_IMPORT)
        cls.import_role = make_key_role(
            cls.tenant, "Importer", "importer",
            (PERM_SHEET_VIEW, PERM_SHEET_ENTER_ANY, PERM_SHEET_IMPORT),
        )
        cls.enter_only_role = make_key_role(
            cls.tenant, "Enter Only", "enter_only", (PERM_SHEET_VIEW, PERM_SHEET_ENTER_ANY),
        )
        cls.importer = cls.make_user("importer@brightfield.test", "Ima", "Porter", cls.import_role)
        cls.lekki_importer = cls.make_user(
            "importer@lekki.test", "Lola", "Porter", cls.import_role, branch=cls.lekki,
        )
        cls.ikeja_importer = cls.make_user(
            "importer@ikeja.test", "Ike", "Porter", cls.import_role, branch=cls.ikeja,
        )
        cls.enter_only = cls.make_user(
            "enteronly@brightfield.test", "Eno", "Only", cls.enter_only_role,
        )
        keyless_role = make_key_role(cls.tenant, "Teacher Without Import", "teacher_no_import", TEACHER_KEYS)
        cls.keyless = cls.make_user(
            "keyless@brightfield.test", "Kemi", "Keyless", keyless_role, branch=cls.lekki,
        )
        TeachingAssignment.all_objects.create(
            tenant=cls.tenant, staff=cls.make_staff(cls.keyless), school_class=cls.jss1b,
            subject=cls.maths, session=cls.year, part=TeachingPart.LEAD,
        )

    @classmethod
    def grant(cls, role, key):
        make_role_permission(role, make_permission(key, scope=PermissionScope.TENANT))

    @classmethod
    def make_staff(cls, user):
        from schools.vs_staff.models import StaffProfile

        return StaffProfile.all_objects.create(
            tenant=cls.tenant, user=user, branch=cls.lekki, employment_status="ACTIVE",
        )

    # ── HTTP ──────────────────────────────────────────────────────────────

    @staticmethod
    def _url(name, term=None, **kwargs):
        url = reverse(name, kwargs=kwargs)
        return url + (f"?term={getattr(term, 'pk', term)}" if term is not None else "")

    def template(self, user, school_class, subject, **kwargs):
        return TenantAPIClient(user).get(
            self._url("gradebook-sheet-template", class_id=school_class.pk, subject_id=subject.pk, **kwargs),
        )

    def upload(
        self, user, school_class, subject, content, *, name="scores.xlsx", term=None,
        content_type=XLSX,
    ):
        """Post *content* (bytes) as the ``file`` of an upload to the sheet."""
        return TenantAPIClient(user).post(
            self._url(
                "gradebook-sheet-import", term, class_id=school_class.pk, subject_id=subject.pk,
            ),
            {"file": SimpleUploadedFile(name, content, content_type=content_type)},
            format="multipart",
        )

    def confirm(self, user, school_class, subject, token, rows, **kwargs):
        return TenantAPIClient(user).post(
            self._url(
                "gradebook-sheet-import-confirm", class_id=school_class.pk,
                subject_id=subject.pk, token=token, **kwargs,
            ),
            {"rows": rows}, format="json",
        )

    def preview(self, user, school_class, subject, rows, headings=HEADINGS, **kwargs):
        """The preview of a workbook of *rows*, asserted to have been checked."""
        response = self.upload(user, school_class, subject, workbook(rows, headings), **kwargs)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    @staticmethod
    def row_numbered(preview, number):
        return next(r for r in preview["rows"] if r["row_number"] == number)

    @staticmethod
    def cell_of(row, component):
        return next(c for c in row["cells"] if c["component"] == component.pk)

    @staticmethod
    def issue_codes(row):
        return [i["code"] for i in row["issues"]]
