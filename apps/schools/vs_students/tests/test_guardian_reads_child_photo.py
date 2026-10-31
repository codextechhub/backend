"""A parent's app shows their child's face; the file route has to agree.

Brightfield has two branches (Lekki and Ikeja) and Sunrise one. Mrs Adaeze
Nwosu is a parent only: her son Tobi is at Ikeja. Mrs Ngozi Eze teaches at
Lekki, holds the student view key for Lekki alone, and her daughter Kemi is at
Ikeja, so at Ikeja she is a parent and nothing more. A parent may open the
passport photograph of her own child on the roll and no other file held
against that child, and being staff never widens or narrows that.
"""
from __future__ import annotations

from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.image_fixtures import photo
from core.media import sign
from schools.vs_students.constants import DocumentType, Relationship, StudentStatus
from schools.vs_students.media_policies import _may_read_student_file
from schools.vs_students.models import Guardian, StudentDocument
from vs_rbac.tests.helpers import make_school_admin

from .base import StudentsFixture

PHOTO = DocumentType.PASSPORT_PHOTO
BIRTH = DocumentType.BIRTH_CERTIFICATE


def _upload(client, tenant, student, document_type):
    name = "face.jpg" if document_type == PHOTO else "birth.pdf"
    content_type = "image/jpeg" if document_type == PHOTO else "application/pdf"
    url = reverse("student-documents", kwargs={"pk": student.pk})
    response = client.post(
        f"{url}?tenant={tenant.slug}",
        {
            "document_type": document_type,
            "file": (
                photo(name) if document_type == PHOTO
                else SimpleUploadedFile(name, b"x" * 32, content_type=content_type)
            ),
        },
        format="multipart",
    )
    assert response.status_code == 201, response.data
    return StudentDocument.all_objects.get(student=student, document_type=document_type)


class _ChildFileFixture(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.nwosu_user = make_school_admin(
            None, email="adaeze.nwosu@example.ng", tenant=cls.tenant,
        )
        cls.nwosu = Guardian.all_objects.create(
            tenant=cls.tenant, full_name="Mrs. Adaeze Nwosu", phone="08110000001",
            email=cls.nwosu_user.email, user=cls.nwosu_user,
        )
        cls.tobi = cls.student(cls, first="Tobi", last="Nwosu", branch=cls.ikeja)
        cls.link(cls, cls.tobi, cls.nwosu, relationship=Relationship.MOTHER)
        # Another family's child at the same branch.
        cls.zainab = cls.student(cls, first="Zainab", last="Yusuf", branch=cls.ikeja)
        # A child of hers who has left the roll.
        cls.dayo = cls.student(
            cls, first="Dayo", last="Nwosu", branch=cls.ikeja,
            status=StudentStatus.WITHDRAWN,
        )
        cls.link(cls, cls.dayo, cls.nwosu, relationship=Relationship.MOTHER, primary=False)

        # A teacher at Lekki who is also the parent of Kemi, at Ikeja.
        cls.eze = cls.lekki_head
        cls.eze_guardian = Guardian.all_objects.create(
            tenant=cls.tenant, full_name="Mrs. Ngozi Eze", phone="08110000002",
            email=cls.eze.email, user=cls.eze,
        )
        cls.kemi = cls.student(cls, first="Kemi", last="Eze", branch=cls.ikeja)
        cls.link(cls, cls.kemi, cls.eze_guardian, relationship=Relationship.MOTHER)
        cls.lekki_pupil = cls.student(cls, first="Ife", last="Bello", branch=cls.lekki)

        cls.pupil_user = make_school_admin(
            None, email="tobi@brightfield.test", tenant=cls.tenant,
        )
        cls.tobi.user = cls.pupil_user
        cls.tobi.save(update_fields=["user"])

        admin = cls.client_for(cls, cls.admin)
        cls.files = {}
        for pupil in (cls.tobi, cls.zainab, cls.dayo, cls.kemi, cls.lekki_pupil):
            cls.files[(pupil.pk, PHOTO)] = _upload(
                admin, cls.tenant, pupil, PHOTO,
            ).file.name
        for pupil in (cls.tobi, cls.zainab, cls.kemi):
            cls.files[(pupil.pk, BIRTH)] = _upload(
                admin, cls.tenant, pupil, BIRTH,
            ).file.name

    def read(self, user, pupil, document_type=PHOTO, *, tenant=None):
        name = self.files[(pupil.pk, document_type)]
        return self.client_for(user).get(
            reverse("stored-media", kwargs={"name": name}),
            {"t": sign(name, user), "tenant": (tenant or self.tenant).slug},
        )


class GuardianReadsChildPhotoTests(_ChildFileFixture):
    def test_a_parent_reads_her_childs_photograph(self):
        self.assertEqual(self.read(self.nwosu_user, self.tobi).status_code, 200)

    def test_a_parent_is_refused_another_familys_photograph(self):
        self.assertEqual(self.read(self.nwosu_user, self.zainab).status_code, 404)

    def test_a_parent_is_refused_her_childs_birth_certificate(self):
        self.assertEqual(self.read(self.nwosu_user, self.tobi, BIRTH).status_code, 404)

    def test_a_parent_is_refused_the_photograph_of_a_child_who_left_the_roll(self):
        self.assertEqual(self.read(self.nwosu_user, self.dayo).status_code, 404)

    def test_unlinking_the_parent_ends_the_reach(self):
        self.nwosu.student_links.filter(student=self.tobi).delete()
        self.assertEqual(self.read(self.nwosu_user, self.tobi).status_code, 404)

    def test_a_closed_parent_account_reads_nothing(self):
        type(self.nwosu_user).objects.filter(pk=self.nwosu_user.pk).update(is_active=False)
        self.nwosu_user.refresh_from_db()
        self.assertEqual(self.read(self.nwosu_user, self.tobi).status_code, 401)

    def test_a_pupil_still_reads_her_own_photograph_and_nothing_else_of_hers(self):
        self.assertEqual(self.read(self.pupil_user, self.tobi).status_code, 200)
        self.assertEqual(self.read(self.pupil_user, self.tobi, BIRTH).status_code, 404)

    def test_a_pupil_is_refused_a_classmates_photograph(self):
        self.assertEqual(self.read(self.pupil_user, self.zainab).status_code, 404)

    def test_the_student_policy_admits_the_parent_of_that_child_only(self):
        request = SimpleNamespace(tenant=self.tenant, user=self.nwosu_user)
        self.assertTrue(_may_read_student_file(request, self.tobi))
        self.assertFalse(_may_read_student_file(request, self.zainab))
        self.assertFalse(_may_read_student_file(request, self.dayo))


class TeacherWhoIsAParentTests(_ChildFileFixture):
    """Mrs Eze's staff reach is Lekki; her daughter is at Ikeja."""

    def test_she_reads_her_own_childs_photograph_at_the_other_branch(self):
        self.assertEqual(self.read(self.eze, self.kemi).status_code, 200)

    def test_she_is_still_refused_another_pupils_photograph_there(self):
        self.assertEqual(self.read(self.eze, self.zainab).status_code, 404)
        self.assertEqual(self.read(self.eze, self.tobi).status_code, 404)

    def test_she_is_refused_her_childs_birth_certificate_there(self):
        self.assertEqual(self.read(self.eze, self.kemi, BIRTH).status_code, 404)

    def test_her_staff_reach_at_her_own_branch_is_unchanged(self):
        self.assertEqual(self.read(self.eze, self.lekki_pupil).status_code, 200)


class StaffReachUnchangedTests(_ChildFileFixture):
    def test_a_school_wide_admin_reads_photographs_and_documents(self):
        self.assertEqual(self.read(self.admin, self.tobi).status_code, 200)
        self.assertEqual(self.read(self.admin, self.tobi, BIRTH).status_code, 200)

    def test_someone_with_no_student_permission_is_refused(self):
        self.assertEqual(self.read(self.nobody, self.tobi).status_code, 404)


class CrossTenantTests(_ChildFileFixture):
    def test_a_parent_at_another_school_cannot_read_the_photograph(self):
        stranger = make_school_admin(
            None, email="bayo.ade@example.ng", tenant=self.solo.tenant,
        )
        Guardian.all_objects.create(
            tenant=self.solo.tenant, full_name="Mr. Bayo Ade", phone="08110000003",
            email=stranger.email, user=stranger,
        )
        response = self.read(stranger, self.tobi, tenant=self.solo.tenant)
        self.assertEqual(response.status_code, 404)


class OneBranchSchoolTests(StudentsFixture):
    """Sunrise has one branch; the rule is the same without a branch to compare."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ade_user = make_school_admin(
            None, email="bayo.ade@example.ng", tenant=cls.solo.tenant,
        )
        cls.ade = Guardian.all_objects.create(
            tenant=cls.solo.tenant, full_name="Mr. Bayo Ade", phone="08110000004",
            email=cls.ade_user.email, user=cls.ade_user,
        )
        cls.bola = cls.student(
            cls, first="Bola", last="Ade", tenant=cls.solo.tenant, branch=cls.solo_branch,
        )
        cls.other = cls.student(
            cls, first="Sade", last="Okoro", tenant=cls.solo.tenant, branch=cls.solo_branch,
        )
        cls.link(cls, cls.bola, cls.ade, relationship=Relationship.FATHER)
        admin = cls.client_for(cls, cls.solo_admin)
        cls.files = {
            (p.pk, t): _upload(admin, cls.solo.tenant, p, t).file.name
            for p in (cls.bola, cls.other) for t in (PHOTO, BIRTH)
        }

    def read(self, user, pupil, document_type=PHOTO):
        name = self.files[(pupil.pk, document_type)]
        return self.client_for(user).get(
            reverse("stored-media", kwargs={"name": name}),
            {"t": sign(name, user), "tenant": self.solo.tenant.slug},
        )

    def test_the_parent_reads_her_childs_photograph_only(self):
        self.assertEqual(self.read(self.ade_user, self.bola).status_code, 200)
        self.assertEqual(self.read(self.ade_user, self.bola, BIRTH).status_code, 404)
        self.assertEqual(self.read(self.ade_user, self.other).status_code, 404)

    def test_staff_are_unchanged(self):
        self.assertEqual(self.read(self.solo_admin, self.other).status_code, 200)
