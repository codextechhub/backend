# XVision Systems Backend

Multi-tenant School Management SaaS backend for the CodeX ecosystem (XVision Systems).
Django 5 + DRF + SimpleJWT + Celery. The authoritative module map is the
**XVS Features RD v2.1** (28 modules across 7 tiers).

## Layout

```
backend/
├── apps/                  # Django project root (run manage.py from here)
│   ├── apps/              # project package: settings/, urls.py, celery.py
│   ├── core/              # response envelope, exception handler, pagination,
│   │                      # mail, thread-locals, management commands
│   ├── vs_schools/        # Module 1  — schools, branches, packages, provisioning
│   ├── vs_admin_console/  # Module 2  — internal backoffice (partial)
│   ├── vs_user/           # Module 3  — identity, JWT auth, sessions, organogram
│   ├── vs_rbac/           # Module 4  — two-layer RBAC + tenant context/managers
│   ├── vs_audit/          # Module 5  — central audit engine
│   ├── vs_config/         # Module 6  — configuration + feature flags
│   ├── vs_workflow/       # Module 7  — approval engine (see its guide.md)
│   ├── vs_notifications/  # Module 8  — in-app + email notifications
│   ├── vs_import_data/    # Module 10 — CSV/XLSX import pipeline
│   ├── vs_finance/        # Module 19 — double-entry GL, AR, banking, payroll,
│   │                      #             budgets, fixed assets, statements, exports
│   ├── vs_procurement/    # Modules 21–23 — vendors, requisitions, POs, GRNs, AP
│   └── vs_payments/       # Module 18 — gateway layer (Paystack/OPay), webhooks
├── cx/                    # project virtualenv (python 3.11)
├── requirements.txt       # pinned dependencies
└── todo.md                # running task list (undone / done)
```

Every app mounts under a versioned prefix in `apps/apps/urls.py` (`/v1/...`).
All responses use the platform envelope `{success, message, data}`; errors are
normalised by `core.exceptions.custom_exception_handler`.

## Environments & settings

| Settings module          | DB         | Use                                    |
|--------------------------|------------|----------------------------------------|
| `apps.settings.local`    | MySQL      | day-to-day development (DB `cx_db`)     |
| `apps.settings.staging`  | PostgreSQL | deployed staging (env-var driven)       |
| `apps.settings.test`     | SQLite     | lightweight tests only — the full migration chain does NOT run on SQLite (vendor-specific raw-SQL migrations); use `local` for full suites |

Required environment variables (server refuses to start without them):
`SECRET_KEY`, `RENDER_API_KEY`, `TEMP_PASSWORD_PEPPER`. See
`apps/apps/settings/base.py` for the optional ones (email, payment providers,
`CORS_ALLOWED_ORIGINS`, …). Never commit real values.

## Getting started

```bash
# The project venv lives at ./cx (its pip shebang is broken — use python -m pip)
./cx/bin/python -m pip install -r requirements.txt

cd apps
../cx/bin/python manage.py migrate --settings=apps.settings.local
../cx/bin/python manage.py runserver --settings=apps.settings.local
```

Useful seed / bootstrap commands (in `core/management/commands/`):

```bash
../cx/bin/python manage.py seed_all_permissions   # RBAC permission catalogue
../cx/bin/python manage.py seed_xvs_modules       # module registry
../cx/bin/python manage.py create_superuser --assign-role --email you@codexng.com
../cx/bin/python manage.py seed_finance --all     # chart of accounts + currencies
```

## Tests

```bash
cd apps
../cx/bin/python manage.py test --settings=apps.settings.local            # everything
../cx/bin/python manage.py test vs_finance --settings=apps.settings.local # one app
```

Note: parts of `vs_rbac/tests/test_views.py`, `test_models.py` and
`test_validators.py` predate several model refactors and are being repaired —
see `todo.md`.

## Tenancy model

Platform → School → Branch → Users/Students/Staff. The School is the tenant
boundary; XVision internal staff (`user_type=CX_STAFF`) are strictly separated
from school users and bypass school scoping.

How the school context flows on a request:

1. `vs_rbac.authentication.TenantJWTAuthentication` (the default DRF auth
   class) validates the JWT, resolves the user's school, sets
   `request.school` **and** the thread-local context.
2. `vs_rbac.middleware.TenantContextMiddleware` does the same for
   session-authenticated paths and always clears the thread-local afterwards.
3. `vs_rbac.permissions.HasRBACPermission` checks `view.rbac_permission`
   (key format `module.resource.action`) against the user's roles in
   `request.school`.
4. Models that declare `objects = TenantAwareManager()` (see
   `vs_rbac/managers.py`) are automatically filtered to the current school;
   `all_objects` is the unscoped escape hatch.

Finance is tenanted separately by **LedgerEntity** (a set of books, optionally
linked to a School via `source_school`). Finance/procurement endpoints take
`?entity=<id|code>` and authorise entity access in
`vs_finance.views.resolve_entity`.

## Conventions

- Money is integer **kobo** (`MoneyField`) — never float.
- Posted journals are immutable; corrections are mirror-image reversals.
- Finance writes its own transactional audit (`FinanceAuditLog`) and mirrors
  best-effort to the central `vs_audit`.
- Async work goes through Celery (`REDIS_URL`); local dev and the current
  staging tier run tasks eagerly (no broker) — see `todo.md` for the worker
  upgrade plan.
- New apps should follow the `vs_user/services/` + thin-views pattern.
