"""Read-only endpoints for the requirements-document library.

Two endpoints: list what exists, and stream one file. There is deliberately no
create/update/delete - the documents are produced by a generator and versioned in
git (see ``documents.py``), so a write endpoint here would be a second, weaker
way to change them.

Both are gated on ``IsPlatformActor`` **and** ``platform.documents.view``. Both
are needed, and the reason is worth stating because the key alone looks
sufficient and is not: ``HasRBACPermission`` matches the key string against roles
on the caller's own tenant without checking which tenant that is, so a
school-tenant role carrying ``platform.documents.view`` would otherwise pass. The
usual backstop - rows being tenant-scoped, so a school actor sees nothing anyway
- does not apply here, because these documents are global CX-internal product
specs describing every customer's system rather than any tenant's data. So the
CX-only boundary is stated explicitly by ``IsPlatformActor``.

The gate is deliberately *not* "is this account on the platform tenant":
``vs_user.models.User`` documents that field as an inert domain marker that must
never drive authorization.
"""
from __future__ import annotations

from django.http import FileResponse
from rest_framework.views import APIView

from core.response import success_response, error_response
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission

from .documents import get_documents, find_version
from .permissions import IsPlatformActor


#: What a .docx is served as. Fixed, not guessed from the extension at request
#: time, because this string is what the browser acts on.
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

VIEW_PERMISSION = "platform.documents.view"


def _serialise(doc) -> dict:
    return {
        "slug": doc.slug,
        "title": doc.title,
        "kind": doc.kind,
        "module_number": doc.module_number,
        "current_version": doc.current.version,
        "current_size_bytes": doc.current.size_bytes,
        "version_count": len(doc.versions),
        "versions": [
            {
                "version": v.version,
                "filename": v.filename,
                "size_bytes": v.size_bytes,
            }
            for v in doc.versions
        ],
    }


class RequirementsDocumentListView(APIView):
    """
    GET /admin/documents/

    Every requirements document in the deployed docs tree, flat and ordered:
    the cross-module MRD first, then the per-module FRDs by module number. Each
    entry carries its current version plus the full version history, so the
    library screen can show one row per document and reveal history on demand
    without a second request.

    Note there is no "last updated" timestamp, and that is on purpose. The only
    date available on disk is the file mtime, which on a deployed server is the
    moment the repo was checked out - identical for all 42 files and unrelated
    to when any document was actually revised. A version label is the honest
    recency signal here; a wrong date would look authoritative and mislead.

    Permission: platform.documents.view

    docstring-name: List requirements documents
    """

    permission_classes = [IsAuthenticatedAndActive & IsPlatformActor & HasRBACPermission]
    rbac_permission = VIEW_PERMISSION

    def get(self, request):
        documents = get_documents()
        return success_response(
            message="Documents retrieved successfully.",
            data={
                "count": len(documents),
                "results": [_serialise(d) for d in documents],
            },
        )


class RequirementsDocumentDownloadView(APIView):
    """
    GET /admin/documents/<slug>/download/[?version=1.2]

    Streams one .docx. Without ``version`` it serves the current one.

    Always a download, never a render: ``Content-Disposition: attachment`` plus
    ``nosniff``, matching what ``core.views.MediaView`` does for non-images. The
    caller needs the bearer token, so the frontend fetches the bytes and saves
    the blob rather than pointing an ``<a href>`` at this URL.

    Permission: platform.documents.view

    docstring-name: Download a requirements document
    """

    permission_classes = [IsAuthenticatedAndActive & IsPlatformActor & HasRBACPermission]
    rbac_permission = VIEW_PERMISSION

    def get(self, request, slug: str):
        requested = request.query_params.get("version") or None
        found = find_version(slug, requested)
        if found is None:
            return error_response(
                message=(
                    f"No version {requested} of that document."
                    if requested
                    else "That document is not in the library."
                ),
                status=404,
            )
        _, version = found

        # Resolved from the registry, never built from the URL - so there is no
        # traversal surface here. Guarded anyway: a file present at scan time can
        # still be gone by the time it is read.
        if not version.path.is_file():
            return error_response(
                message="That document is no longer available on the server.",
                status=404,
            )

        response = FileResponse(
            version.path.open("rb"),
            content_type=DOCX_CONTENT_TYPE,
        )
        response["Content-Length"] = version.size_bytes
        response["Content-Disposition"] = f'attachment; filename="{version.filename}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response
