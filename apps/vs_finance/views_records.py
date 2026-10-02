"""Keeping the books: retention settings, sealed figures, and archived years.

Every endpoint here concerns the whole set of books, never one branch: a seal
covers every branch's figures, a fiscal year binds every branch, and the
retention period is the tenant's. So reading the sealed figures and changing
anything here both need a caller whose reach is the whole tenant, as well as
the key.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import caller_reaches_whole_tenant

from .audit import record
from .constants import FinanceAuditAction
from .money import format_naira
from .serializers import FiscalYearSerializer
from .views import _FiscalCalendarWriteMixin, _resolve_fiscal_year, resolve_entity
from .views_settings import _FinanceSettingsView, _settings_history


def _money(kobo: int) -> dict:
    return {"kobo": kobo, "naira": format_naira(kobo)}


def _assert_whole_tenant_reader(request, entity) -> None:
    """Refuse (403) a caller whose reach is not the whole tenant."""
    if not caller_reaches_whole_tenant(request.user, entity.tenant):
        raise PermissionDenied(
            "Only a school-wide reader can see the sealed figures, because they "
            "cover every branch."
        )


class LedgerSealVerifyView(APIView):
    """GET /finance/seals/verify/?entity=[&fiscal_year=2027] - prove the closed figures.

    Recomputes every current seal (the latest seal of each period and year still
    closed or locked) from the ledger and compares: the seal's own checksum, the
    checksum and count of the ledger lines it covers, and every account's
    balance per branch. ``ok`` is false when anything differs; each failed
    check lists the balances that moved, with what was sealed and what the
    ledger says now. Read-only: nothing is written, and no seal is repaired.

    ``finance.seal.view``, and whole-tenant reach (403 otherwise).

    docstring-name: Verify sealed figures
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.seal.view"

    def get(self, request):
        from vs_tenants.models import Branch

        from .models import Account
        from .seals import describe, verify_entity

        entity = resolve_entity(request)
        _assert_whole_tenant_reader(request, entity)
        fiscal_year = _resolve_fiscal_year(entity, request)
        result = verify_entity(entity, fiscal_years=[fiscal_year] if fiscal_year else None)

        account_ids = {d["account_id"] for c in result.checks for d in c.differences}
        accounts = Account.objects.in_bulk(account_ids)
        branch_ids = {d["branch_id"] for c in result.checks for d in c.differences} - {None}
        branches = Branch.all_objects.in_bulk(branch_ids)

        def difference(row):
            account = accounts.get(row["account_id"])
            branch = branches.get(row["branch_id"])
            return {
                "branch_id": row["branch_id"],
                "branch_name": getattr(branch, "name", None),
                "account_id": row["account_id"],
                "account_code": getattr(account, "code", ""),
                "account_name": getattr(account, "name", ""),
                "sealed": {k: _money(v) for k, v in row["sealed"].items()},
                "now": {k: _money(v) for k, v in row["now"].items()},
            }

        checks = [
            {
                "seal_id": c.seal.pk,
                "label": c.label,
                "kind": c.seal.kind,
                "fiscal_year": c.seal.fiscal_year.year,
                "period_id": c.seal.period_id,
                "sealed_at": c.seal.sealed_at,
                "seal_checksum": c.seal.seal_checksum,
                "line_count": c.seal.line_count,
                "line_count_now": c.line_count_now,
                "ok": c.ok,
                "seal_intact": c.seal_intact,
                "lines_match": c.lines_match,
                "summary": None if c.ok else describe(c),
                "differences": [difference(d) for d in c.differences],
            }
            for c in result.checks
        ]
        return success_response(
            "Sealed figures verified." if result.ok else "Sealed figures differ from the ledger.",
            data={
                "ok": result.ok,
                "checked": len(checks),
                "mismatches": sum(1 for c in checks if not c["ok"]),
                "chain_breaks": result.chain_breaks,
                "checks": checks,
            },
        )


class FiscalYearArchiveView(_FiscalCalendarWriteMixin, APIView):
    """POST /finance/fiscal-years/<id>/archive/?entity= - put a closed year away.

    Body: ``{"reason": str}``, required. The year must be CLOSED or LOCKED and
    at least ``finance.archive.min_age_years`` past its end. Nothing is
    deleted: the year leaves the default lists and pickers and stays readable,
    reportable and exportable with ``?include_archived=true``
    (:mod:`vs_finance.archive`). Audited with the actor and the reason.

    ``finance.fiscalyear.archive``, and whole-tenant reach (403 otherwise).

    docstring-name: Archive a fiscal year
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "finance.fiscalyear.archive"
    archive = True

    def post(self, request, id):
        from .archive import archive_fiscal_year, unarchive_fiscal_year
        from .models import FiscalYear

        entity = resolve_entity(request)
        fiscal_year = FiscalYear.objects.filter(entity=entity, id=id).first()
        if fiscal_year is None:
            raise NotFound("Fiscal year not found for this entity.")
        act = archive_fiscal_year if self.archive else unarchive_fiscal_year
        fiscal_year = act(
            entity, fiscal_year, actor_user=request.user,
            reason=(request.data or {}).get("reason"),
        )
        verb = "archived" if self.archive else "unarchived"
        return success_response(
            f"Fiscal year {fiscal_year.year} {verb}.",
            data=FiscalYearSerializer(fiscal_year).data,
        )


class FiscalYearUnarchiveView(FiscalYearArchiveView):
    """POST /finance/fiscal-years/<id>/unarchive/?entity= - bring an archived year back.

    Body: ``{"reason": str}``, required. Audited with the actor and the reason.
    ``finance.fiscalyear.archive``, and whole-tenant reach (403 otherwise).

    docstring-name: Unarchive a fiscal year
    """

    archive = False


class RecordRetentionSettingsView(_FinanceSettingsView):
    """GET / PATCH /finance/settings/records/?entity= - how long the books are kept.

    GET answers the statutory floor (CodeX's, read-only here), the tenant's own
    retention period (``null`` keeps the floor), the period in force, and the
    minimum age at which a closed year may be archived.

    PATCH accepts ``retention_years`` (a whole number of years no shorter than
    the floor, or ``null`` to keep the floor) and ``archive_min_age_years``.
    Each value is written through :mod:`vs_config`, so its own audit and the
    finance write guard apply, and the change is also written to the finance
    audit trail. ``finance.settings.view`` reads; ``finance.settings.update``
    writes, with whole-tenant reach.

    docstring-name: Record retention settings
    """

    settings_subject = "how long financial records are kept"

    FIELDS = {
        "retention_years": "finance.retention.years",
        "archive_min_age_years": "finance.archive.min_age_years",
    }

    def _payload(self, entity):
        from .archive import min_age_years
        from .retention import TENANT_YEARS_KEY, retention_years, statutory_years
        from vs_config.conf import get_config

        tenant = entity.tenant
        return {
            "statutory_years": statutory_years(),
            "retention_years": get_config(TENANT_YEARS_KEY, None, tenant=tenant),
            "effective_retention_years": retention_years(tenant),
            "archive_min_age_years": min_age_years(tenant),
            "history": _settings_history(entity, FinanceAuditAction.RETENTION_SETTINGS_UPDATED),
        }

    def get(self, request):
        entity = resolve_entity(request)
        return success_response("Record retention settings retrieved.", data=self._payload(entity))

    def patch(self, request):
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import clear_value, set_value

        entity = resolve_entity(request)
        body = request.data or {}
        unknown = sorted(set(body) - set(self.FIELDS))
        if unknown or not body:
            raise ValidationError({
                "settings": f"Provide any of: {', '.join(self.FIELDS)}."
                + (f" Unknown: {', '.join(unknown)}." if unknown else ""),
            })
        before = self._payload(entity)
        with transaction.atomic():
            for field_name, key in self.FIELDS.items():
                if field_name not in body:
                    continue
                definition = ConfigurationDefinition.objects.filter(key=key, is_active=True).first()
                if definition is None:
                    raise NotFound(f"The setting '{key}' is not available.")
                value = body[field_name]
                if value is None:
                    if field_name != "retention_years":
                        raise ValidationError({field_name: "Enter a whole number of years."})
                    clear_value(definition=definition, actor=request.user, tenant=entity.tenant)
                else:
                    set_value(
                        definition=definition, value=value, actor=request.user,
                        tenant=entity.tenant,
                    )
            after = self._payload(entity)
            changed = {f: after[f] for f in self.FIELDS if before[f] != after[f]}
            if changed:
                record(
                    entity=entity, action=FinanceAuditAction.RETENTION_SETTINGS_UPDATED,
                    actor_user=request.user, target_type="LedgerEntity", target_id=entity.pk,
                    message="Record retention settings updated.",
                    before={f: before[f] for f in changed}, after=changed,
                )
        return success_response(
            "Record retention settings saved.", data=self._payload(entity),
        )

