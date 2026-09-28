# config_tenant_clock

A tenant's own clock: which time zone it keeps its calendar in, what time it is
there, and which calendar day it is there. One configuration value,
`display.timezone`, and one small module every app reads it through,
`vs_config.clock`.

The server runs in UTC (`settings.TIME_ZONE = "UTC"`), so `date.today()` and
`timezone.localdate()` answer with the UTC day. For a school in Lagos that day
is wrong for the first hour after every midnight: at 00:30 in Lagos the server
still says it is yesterday. An invoice raised then was dated yesterday, a fee
due yesterday was not yet overdue, and the calendar hub showed the day before.

---

## 1. What it is (and what it is NOT)

- **A day or a wall-clock reading comes from here; an instant does not.** A
  timestamp that is stored (`created_at`, `decided_at`, `posted_at`) stays
  `timezone.now()`, an aware UTC instant. Only "which day is it at this
  tenant" and "what does this tenant's clock say" go through the helper.
- **Every tenant starts on Africa/Lagos.** That is the definition's default
  and the platform's. A school elsewhere sets its own zone on its display
  settings (`docs/schools/school_settings.md`, section on display).
- **Two layers, not three.** `display.timezone` allows `platform` and
  `school` scope. Not `branch`: a school keeps one calendar, and two branches
  disagreeing about the day would put one fee on two due dates.
- **Only real zones are stored.** A write guard registered by `vs_config`
  itself refuses anything that is not in the tz database's own list, at every
  write path (the console's generic value endpoint and the school's display
  endpoint alike). `localtime`, `UTC+1` and `Lagos` are all refused.

## 2. The helper

```python
from vs_config.clock import tenant_zone, tenant_now, tenant_today

tenant_zone(tenant)   # -> zoneinfo.ZoneInfo
tenant_now(tenant)    # -> aware datetime in that zone
tenant_today(tenant)  # -> datetime.date in that zone
```

| Argument | Layer read |
|---|---|
| A school's (or any business) tenant | its own value, else the platform value, else Africa/Lagos |
| The platform tenant (`kind == "PLATFORM"`) | the platform value, else Africa/Lagos |
| `None` | the same as the platform tenant |

Also in the module: `TIME_ZONE_KEY` (`"display.timezone"`),
`DEFAULT_TIME_ZONE` (`"Africa/Lagos"`), `is_valid_time_zone(name)`,
`guard_time_zone` (the write guard) and `forget_tenant_zone(tenant)`.

A stored value that is somehow not a zone (written around the guard, by a raw
update) reads as Africa/Lagos rather than raising.

### Where the tenant comes from

Use the tenant the rows already belong to: `request.tenant` in a view,
`entity.tenant` in the finance engine, `staff.tenant` in staff services,
`row.tenant` on a model. Where no tenant is in reach, pass `None` and the
platform zone applies. Do not thread a tenant through a call chain only for
this: the platform zone is Lagos until somebody changes it, which is right for
every current school.

### Caching

`get_config` is not cached (two queries), so the zone is memoised on the tenant
instance, which lives for one request (`request.tenant`) or one task. Another
instance of the request's own tenant, such as `row.tenant` on each row of a
list, shares the memo held on the request's instance, so a per-row "is this
overdue" asks the configuration once. The platform layer (`None`) is not
memoised. After writing the value, call `forget_tenant_zone(tenant)` so the
same request reads the new zone; the display endpoint does.

A row whose `tenant` has not been loaded still costs one query to load it; add
the tenant to `select_related` on list querysets (`vs_todo` does).

## 3. Seeding

- Data migration `vs_config/migrations/0013_seed_display_timezone.py`: STRING,
  default `"Africa/Lagos"`, `allowed_scopes ["platform", "school"]`,
  INTERNAL. Reversible; the reverse drops the definition and every value.
- `seed_config_catalogue` carries the same row in `SCHOOL_SCOPED_DEFINITIONS`,
  so an environment built from the catalogue has it too.

## 4. Call sites

Every "today" in `vs_calendar`, `vs_academics`, `vs_staff` (except the files
below), `vs_schools` models, the FAL, `vs_finance`, `vs_payments`,
`vs_exports`, `vs_admin_console`, `vs_todo`, `vs_user` organogram,
`vs_tenants` document numbering and `vs_import_data` reads the helper.

Deliberately not swept:

- `vs_health.tasks.rollup_uptime_daily_task`: uptime rollups are the
  platform's own UTC day buckets over UTC probe instants, not anybody's
  calendar.
- `DateField(default=timezone.localdate)` on `vs_staff` and `vs_user`
  position assignments: a field default cannot see its row's tenant, and
  every service that creates these rows passes a date.
- `vs_finance.export_datasets._translate_invoices`: the screen-translation
  hook takes only the query parameters, so its "overdue" bucket uses the
  platform day.
- `vs_import_data` school-import validation ("a subscription cannot expire in
  the past") uses the platform day on purpose: the rows are schools that do not
  exist yet, and every new school starts on the platform zone.
- `vs_user` position assignments belong to platform staff, so their default
  start and end dates are the platform day.

Still to sweep, by the sessions that own them: `vs_procurement` (including
`vendor_portal`, which already reads `display.timezone` with a UTC fallback),
`vs_students`, and three files that held other uncommitted work when the sweep
ran: `schools/vs_schools/serializers.py`, `schools/vs_staff/serializers.py`,
`schools/vs_staff/services/organogram.py`.

## 5. Tests

- `vs_config/tests_clock.py`: Lagos's day at 23:30 UTC, a Nairobi tenant's own
  day and clock, the platform value inherited, the platform tenant reading the
  platform layer, the memo (two queries once, none after, shared with the
  request's tenant), invalid zones refused at the guard, a corrupt stored value
  falling back.
- `schools/vs_calendar/tests/test_school_day.py`: the hub and `current/` turn
  the day at Lagos midnight and at Nairobi midnight; `?on=` still wins.
- `vs_tenants/tests.py` `TenantDocumentNumberTests`: an undated document
  number carries the tenant's day.
