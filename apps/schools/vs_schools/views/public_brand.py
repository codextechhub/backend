"""Public sign-in branding for a school identified by its address.

The page at ``holy-cross.xvs.codexng.com`` needs the school's exact name and
crest before anyone has a session. Both routes take a known slug and refuse a
tenant that cannot sign in. Neither route lists schools or exposes private
profile fields. The name route confirms that a known slug is a school; the logo
route also returns 404 for a school without a crest. Logo bytes come from the
slug's own branding row, never from a caller-selected file path.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import StoredFile
from vs_tenants.models import Tenant

from ..models import School


class PublicSchoolNameView(APIView):
    """Give a known, sign-in eligible school slug its display name.

    The address supplies the slug before there is a session. This route returns
    only the name needed for the sign-in heading and never lists schools.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    tenant_param_required = False
    throttle_scope = "school_brand"

    def get(self, request, slug):
        name = School.objects.filter(
            slug=str(slug or "").strip().lower(),
            tenant__status__in=Tenant.AUTHENTICABLE_STATUSES,
        ).values_list("name", flat=True).first()
        if not name:
            raise NotFound("No school for this address.")

        response = Response({"name": name})
        response["Cache-Control"] = "public, max-age=3600"
        return response


class PublicSchoolLogoView(APIView):
    """GET /i/public/schools/<slug>/logo/ - a school's crest, before sign-in.

    docstring-name: Public school logo
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    tenant_param_required = False
    # Generous because it is a page-load asset shared by a whole school's staff
    # on one office address, and because the response is cacheable so an honest
    # browser asks once an hour.
    throttle_scope = "school_brand"

    def get(self, request, slug):
        # The same lookup sign-in performs, so a school that cannot sign in
        # cannot be probed here either.
        tenant = Tenant.objects.filter(
            slug=str(slug or "").strip().lower(),
            status__in=Tenant.AUTHENTICABLE_STATUSES,
        ).first()
        school = getattr(tenant, "school_profile", None) if tenant else None
        branding = getattr(school, "branding", None) if school else None
        name = getattr(getattr(branding, "logo", None), "name", "") or ""
        if not name:
            raise NotFound("No logo for this school.")

        row = StoredFile.objects.filter(name=name, revoked_at__isnull=True).first()
        if row is None:
            raise NotFound("No logo for this school.")

        response = HttpResponse(
            bytes(row.content), content_type=row.content_type or "image/png",
        )
        response["Content-Length"] = row.size
        # Public, unlike the pay-link crest: this one is painted on a sign-in
        # page anybody can open, so letting a shared cache hold it is the point
        # rather than a leak.
        response["Cache-Control"] = "public, max-age=3600"
        response["X-Content-Type-Options"] = "nosniff"
        return response
