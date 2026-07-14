"""Shared test utilities for the tenant-refactored API surface.

TenantAPIClient goes through the REAL auth layer (TenantJWTAuthentication):
it mints a JWT for the user and appends the mandatory ``?tenant=<slug>``
assertion to every request, so tests exercise the same code path production
traffic takes. Use it instead of ``force_authenticate`` for any endpoint that
reads ``request.tenant`` (entity resolution, tenant-scoped querysets, RBAC).
"""
from __future__ import annotations

from rest_framework.test import APIClient


class TenantAPIClient(APIClient):
    """APIClient that authenticates with a real JWT and asserts one tenant."""

    def __init__(self, user=None, tenant_slug=None, **kwargs):
        super().__init__(**kwargs)
        self._tenant_slug = tenant_slug or (
            user.tenant.slug if user is not None and user.tenant_id else None
        )
        if user is not None:
            from vs_user.tokens import CodeXRefreshToken
            token = CodeXRefreshToken.for_user(user).access_token
            self.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _with_tenant_path(self, path):
        if not self._tenant_slug or "tenant=" in path:
            return path
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}tenant={self._tenant_slug}"

    # GET/HEAD encode ``data`` into the query string, which would override a
    # path-appended parameter — inject into data when it is used.
    def get(self, path, data=None, **extra):
        if self._tenant_slug:
            if data is not None:
                if "tenant" not in data:
                    data = {**data, "tenant": self._tenant_slug}
            else:
                path = self._with_tenant_path(path)
        return super().get(path, data=data, **extra)

    def generic(self, method, path, *args, **kwargs):
        # Body methods (POST/PUT/PATCH/DELETE) keep the path's query string.
        return super().generic(method, self._with_tenant_path(path), *args, **kwargs)
