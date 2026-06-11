"""
Authenticated media serving for the database-backed storage (B9).

GET /media/<name> streams the StoredFile row with its stored content type.
Authentication is required — staff photos and import sheets are not public
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
