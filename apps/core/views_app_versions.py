"""The public route a store app asks, before sign-in, whether its version is acceptable."""
from __future__ import annotations

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import app_versions
from .response import success_response


class AppVersionView(APIView):
    """GET /v1/app-versions/ - whether a store app's version may still run.

    docstring-name: App version check

    Query: ``app`` (the store app id), ``platform`` (``android`` or ``ios``) and
    optionally ``version``. Answers the app's ``minimum``, ``latest`` and
    ``store_url`` with a ``status`` of ``update_required``, ``update_available``
    or ``current``; ``status`` is null when no version was sent. The numbers are
    the same for every tenant and come from server settings, so the route needs
    no session and exposes only what a store listing already shows.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    tenant_param_required = False
    # Answered from settings and cacheable, so this bounds scraping only.
    throttle_scope = "app_versions"
    # Named here so the limit holds whatever the default throttle classes are.
    throttle_classes = [ScopedRateThrottle]

    def get(self, request):
        app = (request.query_params.get("app") or "").strip()
        platform = (request.query_params.get("platform") or "").strip().lower()
        version = request.query_params.get("version")

        if not app or len(app) > 200:
            raise ValidationError({"app": ["The app id is required."]})
        if platform not in app_versions.PLATFORMS:
            raise ValidationError({
                "platform": [f"The platform must be one of {', '.join(app_versions.PLATFORMS)}."],
            })
        if version is not None:
            version = version.strip()
            try:
                app_versions.parse_version(version)
            except ValueError:
                raise ValidationError({"version": ["The version must look like 1.2.3."]})

        policy = app_versions.configured_policy(app, platform)
        if policy is None:
            raise NotFound("No version policy for this app.")

        response = success_response(data={
            "app": app,
            "platform": platform,
            **policy,
            "status": app_versions.version_status(
                version, policy["minimum"], policy["latest"],
            ),
        })
        response["Cache-Control"] = "public, max-age=300"
        return response
