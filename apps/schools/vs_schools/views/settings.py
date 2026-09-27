"""A school's own settings screens: security, and how it runs payroll.

Both settings live in the configuration engine (``vs_config``) and both already
have a console screen there, but none of that is reachable by a school. Every
``config.*`` permission is platform-only, so a school admin can never hold
``config.security.update`` and must never be able to: the same module decides
what a school has bought. These views are the school's door to the two values
that are genuinely the school's to set, gated on the school's own keys
(``school.settings.view`` and ``school.settings.update``) and bound to the
caller's own tenant.

**The tenant is always ``request.tenant``.** There is no slug, pk or ``?tenant=``
of another school to change: the auth layer refuses a school asserting a
foreign tenant, and these views do not opt into ``platform_cross_tenant_param``,
so a platform operator cannot aim them at a school either, except by
impersonating one, in which case they are that school for the request.

**A caller that is not a school gets a 404.** A platform caller acting as
itself resolves to the platform tenant, and for the security form that would
mean writing the platform baseline every school inherits. The check that the
tenant has a school profile is what stops that; the ``allow_platform=False``
passed to the scope resolver is the second line.

**Live schools only.** Neither view declares ``pending_tenant_surface``, so a
school that has not gone live is refused with TENANT_NOT_LIVE, the same as its
notification settings. Neither setting is part of onboarding.

**The rules are the engine's, not restated here.** The security form is saved
through :func:`vs_config.services.curated_settings.save_security_settings`, the
same function the console uses, so the stricter-only rule, the field-keyed 400s
and the ``config.value.updated`` / ``config.value.cleared`` audit events are
identical. Payroll scope is written through
:func:`vs_config.services.resolution.set_value`, so finance's write guard runs
exactly as it does for the console.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import error_response, success_response
from vs_config.exceptions import ConfigurationError
from vs_config.models import ConfigurationDefinition
from vs_config.serializers import SecuritySettingsUpdateSerializer
from vs_config.services.curated_settings import save_security_settings
from vs_config.services.resolution import resolve_value, set_value
from vs_config.services.scopes import resolve_request_scope
from vs_config.runtime_settings import resolve_security_settings
from vs_finance.payroll import (
    PAYROLL_SCOPE_CENTRAL,
    PAYROLL_SCOPE_CHOICES,
    PAYROLL_SCOPE_KEY,
    PAYROLL_SCOPE_PER_BRANCH,
)
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from ..models import School
from ..serializers import PayrollScopeUpdateSerializer


#: The two ways a school can run payroll, worded for the school admin choosing.
PAYROLL_SCOPE_OPTIONS = (
    {
        "value": PAYROLL_SCOPE_CENTRAL,
        "label": "One payroll for the whole school",
        "description": (
            "A single payroll run pays every member of staff, whichever branch "
            "they work at."
        ),
    },
    {
        "value": PAYROLL_SCOPE_PER_BRANCH,
        "label": "Each branch runs its own payroll",
        "description": (
            "Each branch pays its own staff in a separate run, so every active "
            "member of staff must be assigned to a branch before you switch."
        ),
    },
)


class SchoolSettingsView(APIView):
    """Shared plumbing: per-method keys and the caller's own school tenant.

    ``school.settings.view`` reads and ``school.settings.update`` writes, so a
    branch admin (who holds only the first) may look without changing anything.
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self) -> str:
        method = (getattr(self.request, "method", "") or "").upper()
        return (
            "school.settings.view"
            if method in ("GET", "HEAD", "OPTIONS")
            else "school.settings.update"
        )

    def school_tenant(self, request):
        """``request.tenant``, provided it is a school. See the module docstring."""
        tenant = getattr(request, "tenant", None)
        if tenant is None or not School.objects.filter(tenant=tenant).exists():
            raise NotFound("This tenant has no school profile.")
        return tenant


class SchoolSecuritySettingsView(SchoolSettingsView):
    """GET/PATCH /v1/i/me/settings/security/ - the school's sign-in security.

    The body is exactly the console's ``/v1/config/security-settings/``:
    ``settings`` (effective), ``configured`` (as stored), ``sources``,
    ``source_scopes``, ``overrides``, ``compliance`` and ``scope``. Without
    ``?branch=`` the school layer is read and written; with it, that branch's
    layer, which may only be as strict as the school or stricter. The branch
    must belong to this school and be one the caller can see, or the answer is
    a 404 (see :func:`vs_config.services.scopes.resolve_request_scope`).

    ``null`` for a field removes this layer's value, so the field falls back to
    its parent: the school to the platform, a branch to the school.

    docstring-name: My school security settings
    """

    def _scope(self, request):
        self.school_tenant(request)
        return resolve_request_scope(request, allow_platform=False)

    def get(self, request):
        tenant, branch = self._scope(request)
        return success_response(
            "Security settings retrieved.",
            resolve_security_settings(tenant=tenant, branch=branch),
        )

    def patch(self, request):
        tenant, branch = self._scope(request)
        serializer = SecuritySettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = (
            serializer.validated_data.get("reason")
            or "Updated from the school's security settings"
        )
        return success_response(
            "Security settings saved.",
            save_security_settings(
                validated_data=serializer.validated_data,
                actor=request.user,
                reason=reason,
                tenant=tenant,
                branch=branch,
            ),
        )


class SchoolPayrollScopeView(SchoolSettingsView):
    """GET/PATCH /v1/i/me/settings/payroll-scope/ - central or per-branch payroll.

    A school-level setting only: ``payroll.scope`` allows no branch value, so
    ``?branch=`` is not read.

    ``source`` says which layer the answer came from: ``school`` when this
    school has chosen, ``platform`` for a platform value, ``default`` when
    nobody has and the definition's CENTRAL applies.

    A refused switch is a 400 with the refusal keyed on ``scope``::

        {"success": false,
         "message": "<the guard's sentence>",
         "error": {"code": "<the guard's error code>",
                   "detail": {"scope": ["<the guard's sentence>"]}}}

    Finance's guard refuses PER_BRANCH while any active employee has no branch,
    and names up to ten of them in the sentence. The code is
    ``INVALID_CONFIGURATION_VALUE`` for that refusal and for a definition-level
    one; ``INVALID_CONFIGURATION_SCOPE`` if the definition stops allowing a
    school value.

    docstring-name: My school payroll scope
    """

    def _definition(self):
        definition = ConfigurationDefinition.objects.filter(
            key=PAYROLL_SCOPE_KEY, is_active=True,
        ).first()
        if definition is None:
            raise NotFound("Payroll scope is not available.")
        return definition

    def _payload(self, definition, tenant):
        value, row = resolve_value(definition, tenant=tenant)
        if row is None:
            source = "default"
        elif row.scope_key == "platform":
            source = "platform"
        else:
            source = "school"
        return {
            "scope": value if value in PAYROLL_SCOPE_CHOICES else PAYROLL_SCOPE_CENTRAL,
            "source": source,
            "options": [dict(option) for option in PAYROLL_SCOPE_OPTIONS],
        }

    def get(self, request):
        tenant = self.school_tenant(request)
        return success_response(
            "Payroll scope retrieved.",
            self._payload(self._definition(), tenant),
        )

    def patch(self, request):
        tenant = self.school_tenant(request)
        definition = self._definition()
        serializer = PayrollScopeUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = (
            serializer.validated_data.get("reason")
            or "Updated from the school's payroll settings"
        )
        try:
            set_value(
                definition=definition,
                value=serializer.validated_data["scope"],
                actor=request.user,
                tenant=tenant,
                reason=reason,
            )
        except ConfigurationError as exc:
            return error_response(
                exc.message,
                error={"code": exc.error_code, "detail": {"scope": [exc.message]}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return success_response(
            "Payroll scope saved.",
            self._payload(definition, tenant),
        )
