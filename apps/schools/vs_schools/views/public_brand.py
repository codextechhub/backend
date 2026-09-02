"""The one piece of a school's identity that is readable without signing in.

A parent or a member of staff arriving at ``holy-cross.xvs.codexng.com`` sees the
sign-in page before they have any session, and the crest is what tells them they
are at their own school rather than somewhere that merely looks like it. Every
other path to that image needs either a session (the signed media URL is bound to
its reader) or a pay token, so neither can serve this one.

What it deliberately does not do:

* it never lists schools. The slug has to be known before this route says
  anything, which is the difference between confirming a guess and handing over
  a customer list;
* it answers 404 identically for a slug that is not a school, a school whose
  tenant cannot sign in, and a school that has uploaded no crest. So what leaks
  is "this slug has a logo", not "this slug exists";
* it serves the bytes, never a path the caller chose. The file is picked by the
  slug's own branding row, so there is nothing here to point at another
  school's storage.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from core.models import StoredFile
from vs_tenants.models import Tenant


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
