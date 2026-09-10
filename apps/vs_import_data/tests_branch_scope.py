"""Which branch an import belongs to, and who may read it afterwards.

Every batch endpoint resolves its id through
``ImportBatchContextMixin.get_import_batch``, which narrows to the caller's
tenant and then to the branches they hold a grant for. A batch with no branch
belongs to the school as a whole and stays readable from every site.

The file download is tested hardest because it is the payload the rest of the
app only describes. A detail read returns a row count and a status; the file
behind it is the roll itself, with home addresses and guardian telephone
numbers in it, so a lookup that answers only the tenant question hands over far
more than the endpoints beside it would.

Two independent gates decide a batch request, and both are exercised here:
``HasImportBatchRBACPermission`` runs first and may admit a caller on their
module's own import key, and the view's lookup runs second. Narrowing one and
not the other leaves the pair disagreeing about the same id.

The write half is here too, because reading and writing are one rule seen from
either end. A narrowing that nothing ever writes to narrows nothing: every
batch would carry no branch, every batch would be school-wide, and the reads
above would pass while showing each site all of the others.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

from .constants import ImportPermission
from .models import (
    DatasetTypeChoices,
    FileFormatChoices,
    ImportBatch,
    ImportTemplate,
)

#: An id no batch in these tests carries, for the "unknown reference" contrast.
UNKNOWN_BATCH_ID = 9_999_999


class _BranchScopeFixture(TestCase):
    """Bright Star School, its two sites, and one uploaded roll at each.

    A school is created with its main branch already, so Ikeja and Lekki are
    ordinary sites beside it: a school with a single branch cannot demonstrate
    narrowing at all.
    """

    #: Overridden by the fallback case, which grants the students key instead.
    permission_key = ImportPermission.BATCH_VIEW
    dataset_type = DatasetTypeChoices.CX_USERS

    def setUp(self):
        self.school = make_school(slug="bright-star-imports", name="Bright Star School")
        self.ikeja = make_branch(self.school, name="Ikeja Branch", is_main=False)
        self.lekki = make_branch(self.school, name="Lekki Branch", is_main=False)

        self.ada = make_school_admin(self.ikeja, email="ada.okoye@bright-star.test")
        role = make_role(self.school.tenant, name="Import viewer")
        make_role_permission(role, make_permission(self.permission_key))
        # Pinning the assignment is what narrows her. An unpinned grant is
        # whole-tenant reach and the narrowing correctly does not apply.
        make_assignment(self.school.tenant, self.ada, role, branch=self.ikeja)
        self.client = TenantAPIClient(user=self.ada)

        self.template = ImportTemplate.objects.create(
            code=f"branch-scope-{self.dataset_type}",
            name="Branch Scope",
            dataset_type=self.dataset_type,
            default_file_format=FileFormatChoices.CSV,
        )

        self.ikeja_batch = self.make_batch(self.ikeja, "ikeja-roll.csv")
        self.lekki_batch = self.make_batch(self.lekki, "lekki-roll.csv")
        self.school_wide_batch = self.make_batch(None, "school-roll.csv")

    def make_batch(self, branch, filename):
        """A batch whose file names its site, so a leak is visible in the bytes."""
        body = f"Name\n{filename}\n".encode()
        return ImportBatch.objects.create(
            tenant=self.school.tenant,
            branch=branch,
            uploaded_by=self.ada,
            template=self.template,
            dataset_type=self.dataset_type,
            file=SimpleUploadedFile(filename, body),
            file_format=FileFormatChoices.CSV,
            original_filename=filename,
            total_rows=1,
            total_columns=1,
            uploaded_headers=["Name"],
            preview_rows=[{"Name": filename}],
        )

    def download(self, batch_id, client=None):
        return (client or self.client).get(f"/v1/import/batches/{batch_id}/download/")

    def downloaded_bytes(self, response):
        return b"".join(response.streaming_content)


class ImportBatchFileDownloadBranchScopeTests(_BranchScopeFixture):
    """The file endpoint answers the same id the same way the rest of the app does."""

    def test_her_own_sites_file_downloads(self):
        response = self.download(self.ikeja_batch.pk)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ikeja-roll.csv", self.downloaded_bytes(response))

    def test_a_school_wide_upload_is_readable_from_any_site(self):
        """A batch with no branch was uploaded for the school as a whole.

        It is the ordinary shape of an import - nothing on the upload path sets
        a branch - so narrowing that withheld it would take away every file
        every school has.
        """
        response = self.download(self.school_wide_batch.pk)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"school-roll.csv", self.downloaded_bytes(response))

    def test_another_sites_file_is_refused(self):
        """Ada administers Ikeja. Lekki's roll is not hers to read.

        The batch it belongs to already refuses her everywhere else: she cannot
        list it, open it, validate it or read one of its issues. The file was
        the one door left open.
        """
        response = self.download(self.lekki_batch.pk)

        self.assertEqual(response.status_code, 404)

    def test_the_refusal_matches_a_batch_that_does_not_exist(self):
        """Unreachable and absent must be indistinguishable.

        Anything else lets a caller walk the ids and learn how many rolls the
        school has uploaded and where.
        """
        self.assertEqual(
            self.download(self.lekki_batch.pk).status_code,
            self.download(UNKNOWN_BATCH_ID).status_code,
        )

    def test_another_tenants_file_is_refused(self):
        greenfield = make_school(slug="greenfield-imports", name="Greenfield School")
        branch = make_branch(greenfield, name="Greenfield Main", is_main=False)
        stranger = make_school_admin(branch, email="stranger@greenfield.test")
        role = make_role(greenfield.tenant, name="Import viewer")
        make_role_permission(role, make_permission(ImportPermission.BATCH_VIEW))
        make_assignment(greenfield.tenant, stranger, role)

        response = self.download(
            self.ikeja_batch.pk, client=TenantAPIClient(user=stranger),
        )

        self.assertEqual(response.status_code, 404)

    def test_a_school_wide_administrator_still_reads_every_site(self):
        """Whole-tenant reach needs both sources clear.

        An unpinned grant falls back to the holder's home posting, so a user row
        carrying a branch would narrow her even with a school-wide role. Both are
        left empty here, which is what a school-wide administrator looks like.
        """
        obi = make_school_admin(
            None, email="obi.eze@bright-star.test", tenant=self.school.tenant,
        )
        role = make_role(self.school.tenant, name="School-wide import viewer")
        make_role_permission(role, make_permission(ImportPermission.BATCH_VIEW))
        make_assignment(self.school.tenant, obi, role)

        response = self.download(self.lekki_batch.pk, client=TenantAPIClient(user=obi))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"lekki-roll.csv", self.downloaded_bytes(response))

    def test_the_list_offers_only_what_the_file_endpoint_will_serve(self):
        """The two halves of the same question, asked together.

        A file reachable by id but absent from the list is the shape the leak
        took, and it is invisible from either side on its own.
        """
        listed = self.client.get("/v1/import/batches/")

        self.assertEqual(listed.status_code, 200, listed.content)
        ids = {row["id"] for row in listed.json()["data"]}
        self.assertEqual(ids, {self.ikeja_batch.pk, self.school_wide_batch.pk})
        for batch_id in ids:
            self.assertEqual(self.download(batch_id).status_code, 200)


class ImportBatchModuleKeyBranchScopeTests(_BranchScopeFixture):
    """The permission fallback is narrowed too, not only the view's lookup.

    ``HasImportBatchRBACPermission`` lets a module's own import key stand in for
    the generic ``import.batches.*`` ones, and it resolves the batch id itself to
    decide. That second resolution has to ask the same branch question as the
    first, or the two gates disagree about one id and the refusal a caller meets
    depends on which key they happen to hold.
    """

    permission_key = "school.students.import"
    dataset_type = DatasetTypeChoices.STUDENTS

    def test_the_students_key_opens_her_own_sites_file(self):
        response = self.download(self.ikeja_batch.pk)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ikeja-roll.csv", self.downloaded_bytes(response))

    def test_the_students_key_does_not_open_another_site(self):
        response = self.download(self.lekki_batch.pk)

        self.assertEqual(response.status_code, 403)

    def test_the_refusal_matches_a_batch_that_does_not_exist(self):
        self.assertEqual(
            self.download(self.lekki_batch.pk).status_code,
            self.download(UNKNOWN_BATCH_ID).status_code,
        )


class ImportBatchUploadBranchTests(TestCase):
    """The branch a new batch is filed under, and who it is visible to next.

    The uploader decides it, through ``raised_branch``: an import is something a
    person does at a place. What the file goes on to create is a separate
    question with its own rules - a student row lands in the branch its own
    column names - and this is only about the batch, its issues and the file.
    """

    def setUp(self):
        self.school = make_school(slug="upload-branch-school", name="Bright Star School")
        self.ikeja = make_branch(self.school, name="Ikeja Branch", is_main=False)
        self.lekki = make_branch(self.school, name="Lekki Branch", is_main=False)
        self.template = ImportTemplate.objects.create(
            code="upload-branch-students",
            name="Students",
            dataset_type=DatasetTypeChoices.STUDENTS,
            default_file_format=FileFormatChoices.CSV,
        )

    def uploader(self, email, *, home, granted):
        """A staff account holding the upload key, posted and granted as given.

        Both sources matter. An unpinned grant falls back to the holder's home
        posting, so a school-wide uploader needs ``home`` and ``granted`` both
        left empty.
        """
        user = make_school_admin(home, email=email, tenant=self.school.tenant)
        role = make_role(self.school.tenant, name=f"Import uploader {email}")
        make_role_permission(role, make_permission(ImportPermission.BATCH_CREATE))
        for branch in granted or [None]:
            make_assignment(self.school.tenant, user, role, branch=branch)
        return user

    def upload(self, user, **extra):
        payload = {
            "template_id": self.template.pk,
            "file": SimpleUploadedFile("roll.csv", b"Name\nTunde Bello\n"),
            **extra,
        }
        return TenantAPIClient(user=user).post(
            "/v1/import/batches/", payload, format="multipart",
        )

    def test_an_uploader_posted_to_one_site_files_it_there(self):
        ada = self.uploader(
            "ada.okoye@upload.test", home=self.ikeja, granted=[self.ikeja],
        )

        response = self.upload(ada)

        self.assertEqual(response.status_code, 201, response.content)
        batch = ImportBatch.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(batch.branch, self.ikeja)

    def test_a_school_wide_uploader_files_it_school_wide(self):
        """No branch is a real answer, not missing data.

        It is what every school looks like today, and the shape the reads treat
        as shared, so an unbound uploader must keep producing it.
        """
        obi = self.uploader("obi.eze@upload.test", home=None, granted=[])

        response = self.upload(obi)

        self.assertEqual(response.status_code, 201, response.content)
        batch = ImportBatch.objects.get(pk=response.json()["data"]["id"])
        self.assertIsNone(batch.branch)

    def test_an_uploader_covering_two_sites_files_it_school_wide(self):
        """The ambiguous case is answered, not raised as an error.

        An import is as often the school's own spine as one site's roll, and the
        upload carries no branch field for her to answer a question with.
        """
        nkechi = self.uploader(
            "nkechi.ade@upload.test", home=None, granted=[self.ikeja, self.lekki],
        )

        response = self.upload(nkechi)

        self.assertEqual(response.status_code, 201, response.content)
        batch = ImportBatch.objects.get(pk=response.json()["data"]["id"])
        self.assertIsNone(batch.branch)

    def test_naming_another_site_is_refused_rather_than_retargeted(self):
        ada = self.uploader(
            "ada.names-lekki@upload.test", home=self.ikeja, granted=[self.ikeja],
        )

        response = self.upload(ada, branch=self.lekki.pk)

        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(ImportBatch.objects.filter(branch=self.lekki).exists())

    def test_what_one_site_uploads_the_other_cannot_download(self):
        """The two halves meeting, which is the only place the rule is visible.

        Ada administers Ikeja and uploads its roll. Chidi administers Lekki. The
        file holds four hundred children's home addresses and their guardians'
        telephone numbers, and it is not his to open.
        """
        ada = self.uploader(
            "ada.uploads@upload.test", home=self.ikeja, granted=[self.ikeja],
        )
        chidi = make_school_admin(
            self.lekki, email="chidi.eze@upload.test", tenant=self.school.tenant,
        )
        role = make_role(self.school.tenant, name="Lekki import viewer")
        make_role_permission(role, make_permission(ImportPermission.BATCH_VIEW))
        make_assignment(self.school.tenant, chidi, role, branch=self.lekki)

        created = self.upload(ada)
        self.assertEqual(created.status_code, 201, created.content)
        batch_id = created.json()["data"]["id"]

        refused = TenantAPIClient(user=chidi).get(
            f"/v1/import/batches/{batch_id}/download/",
        )

        self.assertEqual(refused.status_code, 404)

    def test_a_call_site_that_names_no_branch_is_a_wiring_error(self):
        """The silent default is what made the narrowing inert in the first place.

        ``context.get("branch")`` returned None for every caller that never set
        it, and None reads as school-wide, so a new upload path could publish one
        site's roll to the whole school and look correct doing it.
        """
        from django.core.exceptions import ImproperlyConfigured

        from .serializers import ImportBatchUploadSerializer

        ada = self.uploader(
            "ada.raw-serializer@upload.test", home=self.ikeja, granted=[self.ikeja],
        )
        request = type("_Request", (), {"tenant": self.school.tenant, "user": ada})()
        serializer = ImportBatchUploadSerializer(
            data={
                "template_id": self.template.pk,
                "file": SimpleUploadedFile("roll.csv", b"Name\nTunde Bello\n"),
            },
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        with self.assertRaises(ImproperlyConfigured):
            serializer.save()
