"""
Authenticated media serving for the database-backed storage (B9).

GET /media/<name> streams the StoredFile row with its stored content type.
Authentication is required - staff photos and import sheets are not public
assets. Image responses are cacheable client-side; spreadsheets are not.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.views import APIView

from core.response import error_response
from vs_rbac.permissions import IsAuthenticatedAndActive

from .models import StoredFile


class MediaView(APIView):
    permission_classes = [IsAuthenticatedAndActive]
    # A school that has not gone live still has a shell to render, and the logo
    # in it is one it uploaded itself during onboarding. Without this the school
    # could set a logo and then be refused the bytes of its own image until
    # go-live - the sidebar, the favicon and the profile preview would all fall
    # back to the bundled default, and the upload would look broken.
    #
    # This grants a pending tenant no reach it would not have the moment it went
    # live: the view is already open to every authenticated user and scopes
    # nothing by tenant, relying instead on the high-entropy suffix that
    # DatabaseStorage gives every stored name.
    pending_tenant_surface = True

    def get(self, request, name: str):
        row = StoredFile.objects.filter(name=name).first()
        if row is None:
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
