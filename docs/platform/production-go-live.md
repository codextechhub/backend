# Production go-live runbook

How production is created, released, rolled back and watched. Production is a
second, separate deployment of the same code that staging runs: its own
database, broker, services and secrets, in Frankfurt. Nothing is shared with
staging.

## Names

| Thing | Production | Staging |
| --- | --- | --- |
| Console (staff) | `intranet.codexng.com` | `intranet-staging.codexng.com` |
| API | `api.codexng.com` | `api-staging.codexng.com` (staging answers on `api.codexng.com` today; it moves when staging is renamed) |
| Schools | `<slug>.xvs.codexng.com` | `<slug>.xvs-staging.codexng.com` |
| Region | Frankfurt | Oregon (stays) |
| Git branch | `production` | `staging` |

A future product `foo` takes `*.foo.codexng.com`, and its staging twin
`*.foo-staging.codexng.com`.

## Git model

- `main` holds the work. `./deploy-staging.sh` resets `staging` to `main`.
- `./release.sh promote` fast-forwards `production` to the commit staging holds
  and tags it `vYYYY.MM.DD.N`. It refuses anything that is not a fast-forward.
- `./release.sh rollback <tag>` adds a commit that restores the tagged files.
  It lists the migrations that arrived after the tag, because the database does
  not roll back with the code.
- Render auto-deploy is **off** for every production service. The script moves
  the branch; the deploy is a click, at a time chosen.

Each migration must stay backward-compatible for one release (add a column
before the code needs it, drop it a release after the code stops using it), so
the previous release still runs on the new schema.

## Creating production on Render (in this order)

Every resource goes in the **Frankfurt** region. Render cannot move a resource
between regions, and services reach the database and Key Value over the private
network only when they share one.

1. **Postgres.** Name `xvs-prod-db`. Start on the smallest paid tier that
   includes automated backups, and read its CPU, memory and connection graphs
   for two weeks before changing size. Copy the *internal* connection details
   into the env group (step 3).
2. **Key Value (Redis).** Name `xvs-prod-redis`. The free tier is enough for the
   task broker. Internal access only.
3. **Env group** `xvs-prod-env`, holding the secrets both services share
   (table below). Generate a fresh `SECRET_KEY`; never reuse staging's.
4. **Web service.** Name `xvs-prod-backend`, branch `production`, auto-deploy
   **off**.
   - Build: `./build.sh`
   - Start: `cd apps && gunicorn apps.wsgi:application`
   - Attach `xvs-prod-env`. Set `DJANGO_SETTINGS_MODULE=apps.settings.staging`
     (the module serves any deployment; its name is historical).
   - The repository's `.python-version` pins Python 3.11.9 for every Render
     service. If `PYTHON_VERSION` is also set in the dashboard, it takes
     precedence and must be exactly `3.11.9`.
   - Health check path: `/v1/health/`-family endpoints require auth, so use a
     cheap public path your Render health check can reach, or leave the default.
5. **Worker.** Name `xvs-prod-worker`, same branch, auto-deploy **off**.
   - Build: `pip install -r requirements.txt`
   - Start: `cd apps && celery -A apps worker -B --loglevel=info --concurrency=2`
   - Same env group and same variables. Confirm the deploy log reports Python
     3.11.9 before starting the worker. Exactly **one** worker while `-B`
     (beat) is embedded; two would double-fire every periodic task.
6. Create the worker and Key Value **before** turning `CELERY_EAGER` to
   `false`. With no broker, every `.delay()` becomes a connection error.
7. **Custom domains** on the web service:
   `api.codexng.com`, `intranet.codexng.com` (if the Console is served from
   here; otherwise it points at the Console host), `*.codexng.com` and
   `*.xvs.codexng.com`. The two wildcards are separate entries: a wildcard
   covers one label only. Add the DNS records Render shows in Cloudflare.
   Cloudflare proxying stays on, as `CLIENT_IP_HEADERS` expects it.

## Environment variables

Per service (not in the group), because they differ per deployment:

| Variable | Production value |
| --- | --- |
| `FRONTEND_BASE_URL` | `https://intranet.codexng.com` |
| `SCHOOL_APP_BASE_URL` | `https://xvs.codexng.com` (scheme and host only; the slug is inserted as a subdomain) |
| `API_PUBLIC_BASE_URL` | `https://api.codexng.com` |
| `HEALTH_PROBE_BASE_URL` | `https://api.codexng.com` |
| `HEALTH_SSL_DOMAIN` | `api.codexng.com` |
| `CELERY_EAGER` | `false` |
| `REDIS_URL` | the Key Value internal connection string |

The service refuses to start if any base URL is missing.

In the env group:

| Variable | Notes |
| --- | --- |
| `SECRET_KEY` | new, long, random |
| `ALLOWED_HOSTS` | `api.codexng.com,intranet.codexng.com,.xvs.codexng.com` plus the Render hostname while testing |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | from the production Postgres, internal host |
| `CORS_ALLOWED_ORIGINS` | `https://intranet.codexng.com` |
| `CORS_ALLOWED_ORIGIN_REGEXES` | default already matches `*.xvs.codexng.com`; leave unset |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | Zoho account; `EMAIL_HOST` defaults to `smtp.zoho.com`, port 587 with TLS |
| `PAYSTACK_PUBLIC_KEY`, `PAYSTACK_SECRET_KEY` | **live** keys from the active Paystack account |
| `PLATFORM_ISSUER_NAME` and the other `PLATFORM_ISSUER_*` | the company details printed on platform invoices |
| `SUPERUSER_EMAIL`, `SUPERUSER_PASSWORD` | only if the build creates the superuser (see below) |

## First deploy

1. Push `main`, run `./deploy-staging.sh`, confirm staging works.
2. `./release.sh promote`. The first run creates the `production` branch.
3. In Render, press Deploy on the web service, watch the build log. It runs
   `migrate` and the idempotent seed commands (permissions, notification types
   and templates, settings, config catalogue, packages, record history).
4. Press Deploy on the worker.
5. **Create the first platform superuser by hand** in the web service's Render
   shell, with a real password, then keep it out of the build:

   ```bash
   cd apps && python manage.py create_superuser --email <you> --password '<strong>' \
     --first-name <first> --last-name <last>
   ```

   The commented block in `build.sh` carries a known default password. Do not
   enable it for production.
6. Confirm `core.W001` does not appear in the logs (it warns when Celery is
   eager in a non-debug deployment).

## Paystack and Zoho

- **Paystack webhook.** In the Paystack dashboard (live mode), set the webhook
  URL to `https://api.codexng.com/v1/payments/webhooks/paystack/`. Paystack
  signs the call with the secret key, so the live secret key must be the one in
  the env group.
- **Zoho.** Send a test activation mail from the Console and confirm it lands
  outside spam. Add SPF and DKIM records for the sending domain in Cloudflare
  DNS from Zoho's admin console.

## Smoke test, in order

1. API answers over HTTPS, and `http://` redirects.
2. Sign in to the Console as the superuser.
3. Create a school tenant. Its address is `<slug>.xvs.codexng.com`; open it and
   sign in as that school's admin. Mail arrives.
4. A small live Paystack payment completes, the webhook is received, and the
   ledger shows it.
5. The worker log shows beat scheduling tasks, and the health overview reports
   the probes.

## Watching it

- **Health** (`vs_health`) answers "is it alive": probes, certificate expiry.
  Its probes run from the worker they watch, so add an **external uptime
  ping** (UptimeRobot or similar) on the API address as the second opinion.
- **Sentry**, region **EU** (the data region cannot change after the
  organisation is created). One project each for the backend (Django) and the
  two frontends (React). Reports carry the exception, stack, route and release,
  and no personal data (see `core.observability` and each frontend's
  `src/utils/sentry.ts`). Reporting is off wherever the DSN is empty.

  | Where | Variables |
  | --- | --- |
  | Backend web and worker | `SENTRY_DSN`, `SENTRY_ENVIRONMENT` (`production` or `staging`). The release is Render's commit, read automatically. |
  | Console and school builds | `VITE_SENTRY_DSN`, `VITE_SENTRY_ENVIRONMENT`, optionally `VITE_SENTRY_RELEASE` (the git commit) |

  A DSN with no environment name makes the service refuse to start, so a
  staging error is never filed as production. Staging and production use the
  same Sentry projects and differ only by environment.
- **Backups.** Confirm the retention window of the chosen Postgres tier and
  record the restore steps here once rehearsed. A rollback of code never
  undoes a migration; a database restore does.

## Renaming staging

Done after production is live, so the real names are never in limbo. Change
`FRONTEND_BASE_URL`, `SCHOOL_APP_BASE_URL`, `API_PUBLIC_BASE_URL`,
`HEALTH_PROBE_BASE_URL`, `HEALTH_SSL_DOMAIN`, `ALLOWED_HOSTS`,
`CORS_ALLOWED_ORIGINS` and `CORS_ALLOWED_ORIGIN_REGEXES` (its default matches
only `*.xvs.codexng.com`, so staging must set it to `*.xvs-staging.codexng.com`)
on the staging services, move the Cloudflare records, and add the matching
custom domains on the staging web service.

## CSRF cookie name

`CSRF_COOKIE_DOMAIN` defaults to `.codexng.com`, a parent of both deployments,
so a browser holding both would send one deployment's token to the other.
Narrowing the domain cannot fix it, because the Console and the API are
siblings and share only `.codexng.com`. Each deployment that has a twin names
its own cookie instead:

| | Backend env | Frontend build env |
| --- | --- | --- |
| Production | nothing (default `csrftoken`) | nothing |
| Staging | `CSRF_COOKIE_NAME=csrftoken_staging` | `VITE_CSRF_COOKIE_NAME=csrftoken_staging` on both the Console and school builds |

The two values must match. Set them on staging when it is renamed.

The frontends' committed `.env` files point `VITE_BACKEND_URL` at
`https://api.codexng.com/v1`. Set it as a build variable on each hosting
project (`https://api-staging.codexng.com/v1` for staging) so a staging build
never calls production.
