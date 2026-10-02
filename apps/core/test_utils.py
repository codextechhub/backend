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
    # path-appended parameter - inject into data when it is used.
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


class append_only_unlocked:
    """Lift the append-only triggers on ``table`` for the body of a test, and only there.

    The audit trails refuse every update and delete at the database
    (:mod:`core.append_only`). A test that needs an empty trail or a reshaped
    fixture row says so with this, inside the transaction the test case already
    holds: PostgreSQL's DDL is transactional, so the triggers come back at the
    end of the block and the rollback at the end of the test would restore
    them regardless. Never used outside tests.
    """

    def __init__(self, table="vs_audit_auditevent"):
        self.table = table

    def _set(self, state):
        from django.db import connection

        with connection.cursor() as cursor:
            # Settle deferred foreign-key checks first: ALTER TABLE refuses while any are pending.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(f'ALTER TABLE "{self.table}" {state} TRIGGER USER')
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")

    def __enter__(self):
        self._set("DISABLE")
        return self

    def __exit__(self, *exc):
        self._set("ENABLE")
        return False


def empty_audit_trail() -> None:
    """Delete every platform audit event inside the current test's transaction."""
    from vs_audit.models import AuditEvent

    with append_only_unlocked():
        AuditEvent.objects.all().delete()
