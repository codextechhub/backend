"""
Authenticated media serving for the database-backed storage (B9).

``GET /media/<name>?t=<signature>`` streams one ``StoredFile``, and only to a
caller who clears all four gates: a live session, a signature issued to *them*
and not yet expired, the tenant the file belongs to, and the owning record's own
read policy. :mod:`core.media` explains what each gate catches and why none of
them is sufficient alone.

Every refusal is a 404, including the ones that are really "not yours". A 403
would confirm that a name exists, which is exactly the fact a stale or forwarded
link is fishing for.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.views import APIView

from core.response import error_response
from vs_rbac.permissions import IsAuthenticatedAndActive

from . import media
from .models import StoredFile


class MediaView(APIView):
    permission_classes = [IsAuthenticatedAndActive]
    # A school that has not gone live still renders a shell, and the logo in it
    # is one it uploaded during onboarding. Refusing the bytes would drop the
    # sidebar, favicon and profile preview back to the bundled default and make
    # the upload look broken.
    #
    # This grants a pending tenant no reach it would not have at go-live: what
    # it may read is decided by the file's own tenant and its owning record's
    # policy, which are the same on both sides of that line.
    pending_tenant_surface = True

    def get(self, request, name: str):
        # Decide before loading the bytes. ``content`` can be tens of megabytes,
        # and a refused request has no business pulling one out of the database
        # to throw it away - which is also what a scan of stale links would do.
        row = StoredFile.objects.defer("content").filter(name=name).first()
        if row is None or not media.authorize(request, row):
            return error_response(message="File not found.", status=404)
        response = HttpResponse(
            bytes(row.content),
            content_type=row.content_type or "application/octet-stream",
        )
        response["Content-Length"] = row.size
        response["X-Content-Type-Options"] = "nosniff"
        if (row.content_type or "").startswith("image/"):
            response["Cache-Control"] = "private, max-age=86400"
        else:
            response["Content-Disposition"] = f'attachment; filename="{name.rsplit("/", 1)[-1]}"'
        return response
