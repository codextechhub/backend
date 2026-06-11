"""
JWT authentication that also establishes the school (tenant) context.

Why this exists
---------------
Django middleware runs BEFORE DRF authentication. SimpleJWT authenticates
inside the view layer, so ``TenantContextMiddleware`` only ever sees an
anonymous user on API requests — ``request.school`` stayed ``None`` and the
thread-local school used by ``TenantAwareManager`` was never set.

This class closes that gap: the moment a token validates, it resolves the
user's school, stamps it onto the underlying ``HttpRequest`` and into
thread-local storage. ``TenantContextMiddleware`` still owns the cleanup —
its ``finally`` block clears the thread-local after the response has passed
back through the middleware stack, which also covers context set here.

Wired in via ``REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]``.
"""
from __future__ import annotations

from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from core.thread_locals import set_current_school


class TenantJWTAuthentication(JWTAuthentication):

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, validated_token = result

        school = None
        # Vision staff bypass school scoping entirely.
        if getattr(user, "user_type", None) != "CX_STAFF":
            school_id = getattr(user, "school_id", None)
            if school_id:
                from vs_schools.models import School

                school = School.objects.filter(pk=school_id).first()
                if school is None:
                    raise AuthenticationFailed("User's school does not exist.")

        # Stamp the underlying HttpRequest so middleware, permissions and
        # views all read the same value (the DRF Request proxies to it).
        django_request = getattr(request, "_request", request)
        django_request.school = school
        if school is not None:
            set_current_school(school)

        return user, validated_token
