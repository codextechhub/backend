"""Requirements-document library - authorization first, then the parser.

The security group leads because this library is CX-internal product
documentation: it describes how the whole platform works, for every tenant. A
school user reaching it would be a disclosure bug, not a cosmetic one, so the
tenant-isolation case is pinned explicitly rather than assumed from the fact
that the key happens to live in the `platform` module.

The parser group runs against a temporary docs tree rather than the real one, so
the tests stay green when a document is added, revised or renamed - what is being
pinned is the naming *contract* from `docs/frd/README.md`, not today's file list.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from vs_rbac.models import (
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.tests.helpers import (
    make_branch,
    make_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_user.tokens import CodeXRefreshToken

from .documents import MRD_SLUG, clear_cache, get_documents

LIST_URL = "/v1/admin/documents/"
PERM_VIEW = "platform.documents.view"


def grant(user, *keys):
    """Give *user* an active role on their own tenant carrying *keys*."""
    role, _ = TenantRoleTemplate.objects.get_or_create(
        tenant=user.tenant, key=f"docs-test-{user.pk}",
        defaults={"name": f"Docs Test Role {user.pk}", "status": "ACTIVE"},
    )
    for key in keys:
        TenantRolePermission.objects.get_or_create(
            role=role, permission=make_permission(key),
        )
    TenantUserRoleAssignment.objects.get_or_create(
        tenant=user.tenant, user=user, role=role,
        defaults={"assignment_status": "ACTIVE"},
    )
    return role


def build_docs_tree(root: Path) -> None:
    """Write a miniature docs tree covering every naming case in the real one.

    Byte content is irrelevant to the parser, but the sizes are made distinct so
    a test can prove the reported size belongs to the file it names.
    """
    mrd = root / "module-requirements"
    mrd.mkdir(parents=True)
    # Deliberately spans the version shapes that break string sorting.
    for version, size in [("2.9", 10), ("2.9.1", 11), ("2.10", 12), ("2.15", 13)]:
        (mrd / f"XVS_Module_Requirements_Document_v{version}.docx").write_bytes(b"x" * size)
    # The older name for the same lineage.
    (mrd / "XVS_Module_FR_Breakdown_v2.5.docx").write_bytes(b"x" * 9)

    frd = root / "functional-requirements"
    (frd / "01-school-and-branch-management").mkdir(parents=True)
    (frd / "23-purchase-orders-delivery-and-ap").mkdir(parents=True)

    m01 = frd / "01-school-and-branch-management"
    (m01 / "XVS_M01_School_and_Branch_Management_Functional_Requirements_Document_v1.2.docx").write_bytes(b"x" * 20)
    (m01 / "XVS_M01_School_and_Branch_Management_Functional_Requirements_Document_v1.1.docx").write_bytes(b"x" * 21)
    # The one pre-convention filename still in the real tree.
    (m01 / "01_School_and_Branch_Management_FRD_v1.0.docx").write_bytes(b"x" * 22)

    m23 = frd / "23-purchase-orders-delivery-and-ap"
    (m23 / "XVS_M23_Purchase_Orders_Delivery_and_AP_Functional_Requirements_Document_v1.3.docx").write_bytes(b"x" * 30)

    # Must be ignored: the library is .docx only.
    (mrd / "notes.md").write_text("not a document")


class DocumentLibraryTestBase(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        build_docs_tree(self.root)
        self._override = override_settings(REQUIREMENTS_DOCS_ROOT=str(self.root))
        self._override.enable()
        clear_cache()

        self.school = make_school(slug="docs-school", name="Docs School")
        self.branch = make_branch(self.school)
        self.user = make_vision_user(email="docs-cx@codex.test")

    def tearDown(self):
        self._override.disable()
        clear_cache()
        self._tmp.cleanup()

    def client_for(self, user):
        client = APIClient()
        token = CodeXRefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def get(self, url, user=None):
        user = user or self.user
        joiner = "&" if "?" in url else "?"
        return self.client_for(user).get(f"{url}{joiner}tenant={user.tenant.slug}")


class DocumentPermissionTests(DocumentLibraryTestBase):
    """Nobody reaches the library without platform.documents.view."""

    def test_anonymous_is_rejected(self):
        resp = APIClient().get(LIST_URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_anonymous_cannot_download(self):
        resp = APIClient().get(f"{LIST_URL}{MRD_SLUG}/download/")
        self.assertIn(resp.status_code, (401, 403))

    def test_cx_user_without_the_key_is_denied(self):
        self.assertEqual(self.get(LIST_URL).status_code, 403)

    def test_cx_user_with_the_key_is_allowed(self):
        grant(self.user, PERM_VIEW)
        self.assertEqual(self.get(LIST_URL).status_code, 200)

    def test_download_needs_the_key_too(self):
        """The list gate is not the only gate - the bytes carry their own."""
        resp = self.get(f"{LIST_URL}{MRD_SLUG}/download/")
        self.assertEqual(resp.status_code, 403)

    def test_a_school_tenant_cannot_even_be_granted_the_key(self):
        """The disclosure case: these documents describe every tenant's internals.

        ``platform.documents.view`` is declared ``PermissionScope.PLATFORM``, so
        a school-tenant grant of the same string is refused at the model - the
        authority cannot be manufactured inside a tenant at all. This used to
        rest on the key merely being seeded on the codex tenant, which stopped
        nothing: any school admin holding the override or role-create key could
        write the row themselves.
        """
        from django.core.exceptions import ValidationError

        admin = make_school_admin(self.branch, email="docs-school-admin@test.com")
        with self.assertRaises(ValidationError):
            grant(admin, PERM_VIEW)

    def test_school_user_cannot_reach_the_library(self):
        admin = make_school_admin(self.branch, email="docs-school-read@test.com")
        resp = self.get(LIST_URL, user=admin)
        self.assertEqual(
            resp.status_code, 403,
            "a school-tenant user read the CX-internal requirements library",
        )

    def test_school_user_cannot_download(self):
        admin = make_school_admin(self.branch, email="docs-school-dl@test.com")
        resp = self.get(f"{LIST_URL}{MRD_SLUG}/download/", user=admin)
        self.assertEqual(resp.status_code, 403)


class DocumentListTests(DocumentLibraryTestBase):
    def setUp(self):
        super().setUp()
        grant(self.user, PERM_VIEW)

    def payload(self):
        resp = self.get(LIST_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]

    def test_one_row_per_document_not_per_file(self):
        """9 files in the tree collapse to 3 documents."""
        data = self.payload()
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 3)

    def test_mrd_sorts_first_then_frds_by_module_number(self):
        slugs = [d["slug"] for d in self.payload()["results"]]
        self.assertEqual(
            slugs,
            [
                MRD_SLUG,
                "01-school-and-branch-management",
                "23-purchase-orders-delivery-and-ap",
            ],
        )

    def test_current_version_compares_numerically_not_as_a_string(self):
        """v2.15 is current, not v2.9 - the bug string sorting would introduce."""
        mrd = self.payload()["results"][0]
        self.assertEqual(mrd["current_version"], "2.15")

    def test_version_history_is_newest_first(self):
        mrd = self.payload()["results"][0]
        self.assertEqual(
            [v["version"] for v in mrd["versions"]],
            ["2.15", "2.10", "2.9.1", "2.9", "2.5"],
        )

    def test_legacy_named_file_is_kept_as_history_not_dropped(self):
        m01 = next(d for d in self.payload()["results"] if d["module_number"] == 1)
        self.assertEqual(m01["version_count"], 3)
        self.assertIn("1.0", [v["version"] for v in m01["versions"]])

    def test_non_docx_files_are_ignored(self):
        for doc in self.payload()["results"]:
            for version in doc["versions"]:
                self.assertTrue(version["filename"].endswith(".docx"))

    def test_title_is_readable_and_keeps_its_acronyms(self):
        m23 = next(d for d in self.payload()["results"] if d["module_number"] == 23)
        self.assertEqual(m23["title"], "Purchase Orders Delivery and AP")

    def test_size_reported_is_the_current_version_size(self):
        m23 = next(d for d in self.payload()["results"] if d["module_number"] == 23)
        self.assertEqual(m23["current_size_bytes"], 30)

    def test_empty_docs_root_returns_an_empty_library_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            with override_settings(REQUIREMENTS_DOCS_ROOT=empty):
                clear_cache()
                data = self.payload()
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["results"], [])


class DocumentDownloadTests(DocumentLibraryTestBase):
    def setUp(self):
        super().setUp()
        grant(self.user, PERM_VIEW)

    def test_download_without_a_version_serves_the_current_one(self):
        resp = self.get(f"{LIST_URL}{MRD_SLUG}/download/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "XVS_Module_Requirements_Document_v2.15.docx",
            resp["Content-Disposition"],
        )

    def test_download_can_name_an_older_version(self):
        resp = self.get(f"{LIST_URL}{MRD_SLUG}/download/?version=2.9")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "XVS_Module_Requirements_Document_v2.9.docx",
            resp["Content-Disposition"],
        )

    def test_it_downloads_and_never_renders(self):
        """The behaviour the library was asked for: click, save, open beside you."""
        resp = self.get(f"{LIST_URL}{MRD_SLUG}/download/")
        self.assertTrue(resp["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            resp["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def test_bytes_match_the_file_on_disk(self):
        resp = self.get(f"{LIST_URL}23-purchase-orders-delivery-and-ap/download/")
        self.assertEqual(b"".join(resp.streaming_content), b"x" * 30)

    def test_unknown_document_is_404_not_500(self):
        resp = self.get(f"{LIST_URL}no-such-module/download/")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_version_is_404(self):
        resp = self.get(f"{LIST_URL}{MRD_SLUG}/download/?version=9.9")
        self.assertEqual(resp.status_code, 404)

    def test_path_traversal_cannot_be_expressed(self):
        """A slug is a registry lookup key, never a path fragment.

        Django's slug converter rejects dots and slashes outright, so these do
        not resolve to this view at all - and even a slug that routed would miss
        the registry and 404 rather than reach the filesystem.
        """
        for attempt in ("../../settings", "..%2f..%2fsettings", "etc-passwd"):
            resp = self.get(f"{LIST_URL}{attempt}/download/")
            self.assertEqual(resp.status_code, 404, f"{attempt} did not 404")


class DocumentRegistryTests(DocumentLibraryTestBase):
    """Registry-level behaviour with no HTTP in the way."""

    def test_cache_is_scoped_to_the_docs_root(self):
        """Switching roots must re-scan, or tests would leak into each other."""
        first = get_documents()
        self.assertEqual(len(first), 3)
        with tempfile.TemporaryDirectory() as empty:
            with override_settings(REQUIREMENTS_DOCS_ROOT=empty):
                self.assertEqual(get_documents(), [])
        self.assertEqual(len(get_documents()), 3)

    def test_missing_docs_root_is_survivable(self):
        """A deploy that shipped without docs/ must degrade, not 500."""
        with override_settings(REQUIREMENTS_DOCS_ROOT="/nonexistent/docs/frd"):
            clear_cache()
            self.assertEqual(get_documents(), [])
