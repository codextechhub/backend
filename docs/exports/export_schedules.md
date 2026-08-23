# export_schedules

Unattended runs: the schedule row, the occurrence maths that keeps a 03:00 Lagos
export at 03:00 through a clock change, the dispatcher that starts due
schedules, the three ways a schedule stops, and the difference between paused
and finished.

Routes covered here (`/v1/exports/`): `schedules/`, `schedules/<pk>/`,
`schedules/<pk>/pause/`, `schedules/<pk>/resume/`.

Findings live in **`error/exports/export_code_issues.md`**.

---

## 1. What it is (and what it is NOT)

- **Local time plus a timezone name, never a stored UTC offset**
  (`models.py:213-218`, `scheduling.py:3-7`). That is what keeps a 03:00 Lagos
  run at 03:00 when the clocks move. `next_run_at` is materialised in UTC purely
  so the dispatcher has something to index, and it is always recomputed from the
  local fields by `reschedule` (`models.py:276-290`).
- **A schedule runs as the definition's owner, not as whoever created it**
  (`models.py:219-221`, `services.py:920-923`). An unattended run must never read
  more than its owner could read by hand.
- **PAUSED and FINISHED are different states, deliberately**
  (`constants.py:162-170`). Paused means something went wrong and a person must
  act; finished means the series simply ran out - a `ONCE` schedule that fired,
  or one past its end date. Keeping them apart is what stops a completed
  schedule sitting in the paused list looking like a fault.
- **A broken export stops filling the Files list.** The third consecutive failure
  pauses the schedule and records why (`models.py:292-306`,
  `constants.py:179-181`). Without it a broken export quietly produces one
  identical failure a night for a month.
- **A window missed through an outage runs once on recovery, inside a
  six-hour grace period, and is otherwise skipped and reported**
  (`constants.py:183-185`, `scheduling.py:131-136`). Catching up six stale
  nightly files helps nobody.
- **State is not writable through PATCH.** `state`, `pause_reason`,
  `pause_detail`, `consecutive_failures` and `next_run_at` are read-only on the
  serializer (`serializers.py:526-529`); a schedule is paused and resumed through
  its own endpoints, which audit the change. Letting PATCH set the state would
  give two ways to pause with only one of them recorded. Test: `tests.py:2081`.
- **The editor reads the schedule back in words, not in cron.** `describe`
  (`scheduling.py:139-175`) produces "runs every day at 03:00 (Africa/Lagos),
  starting 01 Aug 2026, with no end date. A clock change keeps the local time
  fixed." The UI shows it verbatim.
- **This is not a cron surface.** There is no expression, no seconds, no
  multiple times per day, and no "last business day". Five recurrences and one
  time.

## 2. Domain model

### `ExportSchedule` (`models.py:211`)

| Field | Meaning |
|---|---|
| `definition` | CASCADE. The recipe that runs |
| `recurrence` | `ONCE` `DAILY` `WEEKLY` `MONTHLY` `QUARTERLY` (`constants.py:152-159`) |
| `day` | 1-31 for MONTHLY/QUARTERLY, 0-6 (Mon-Sun) for WEEKLY, ignored otherwise |
| `at_time` | Local wall-clock time |
| `timezone_name` | IANA name, default `Africa/Lagos` |
| `starts_on`, `ends_on` | Date bounds; `ends_on` null means no end |
| `skip_when_empty` | Declared, published, and read by nothing - see §8 |
| `state` | `ACTIVE` `PAUSED` `FINISHED` |
| `pause_reason` | `BY_PERSON` `CONSECUTIVE_FAILURES` `OWNER_INACTIVE` (`constants.py:173-176`) |
| `pause_detail` | Free text, truncated to 300 |
| `consecutive_failures` | Reset by a good run and by resume |
| `next_run_at` | Derived UTC index for the dispatcher, `db_index=True` |
| `last_run` | SET_NULL to the run |

`Meta.ordering = ["next_run_at"]`, index on `(state, next_run_at)`
(`models.py:268-270`) - which is exactly the dispatcher's query.

Three methods carry the behaviour:

- `reschedule(after=None)` (`models.py:276-290`): sets `next_run_at` to the first
  occurrence strictly after `after`, and flips an ACTIVE schedule with no future
  occurrence to **FINISHED** rather than paused.
- `register_failure(detail)` (`models.py:292-306`): increments, and at
  `MAX_CONSECUTIVE_FAILURES` (3) sets PAUSED with reason
  `CONSECUTIVE_FAILURES` and the detail.
- `register_success()` (`models.py:308-312`): clears the counter, and writes only
  if it was non-zero.

## 3. Endpoint map

| Route | Method | `rbac_permission` | View |
|---|---|---|---|
| `schedules/` | GET, POST | `schedule.view` **or** `schedule.create` | `ScheduleListView` (`views.py:949`) |
| `schedules/<pk>/` | GET, PATCH, DELETE | `schedule.view` **or** `schedule.manage` | `ScheduleDetailView` (`views.py:1005`) |
| `schedules/<pk>/pause/` | POST | `exports.schedule.manage` | `SchedulePauseView` (`views.py:1038`) |
| `schedules/<pk>/resume/` | POST | `exports.schedule.manage` | `ScheduleResumeView` (`views.py:1067`) |

Pause and resume are one view with a `resume` class flag (`views.py:1042`,
`views.py:1067`), so the two paths cannot drift apart.

### Query parameters actually read

`GET /schedules/` (`views.py:954-963`): `?state=` (upper-cased) and
`?definition=` (an id - see `export_code_issues` §6).

### Request bodies actually read

`ExportScheduleSerializer` (`serializers.py:505`) writes `definition`,
`recurrence`, `day`, `at_time`, `timezone_name`, `starts_on`, `ends_on`,
`skip_when_empty`. Everything else in the field list is read-only.

`POST /schedules/<pk>/pause/` reads `reason` from the raw body
(`views.py:1058-1060`), truncated to 300 characters by `pause_schedule`.
Resume reads nothing.

Validation (`serializers.py:544-577`):

- `timezone_name` must be a zone this server knows, checked with `ZoneInfo`
  (`serializers.py:544-554`). Test: `tests.py:2050`.
- weekly `day` is 0-6; monthly/quarterly `day` is 1-31, with the error message
  stating the short-month rule.
- `ends_on` cannot precede `starts_on`. Test: `tests.py:2058`.

Creation adds two rules in the view (`views.py:976-985`): the definition must be
one the caller could **edit** (`for_write=True` - an unattended run makes its
owner answerable for output nobody asked for that morning), and it must be
neither a draft nor archived.

### Response shape

Alongside the stored fields, every schedule publishes `definition_name`,
`owner_name`, `last_run` (`{reference, status, at}`) and `reads_as` - the plain
sentence from `describe` (`serializers.py:536-542`).

## 4. Lifecycle / state machine

```
POST /schedules/            → ACTIVE, reschedule() computes next_run_at
      │
      ├── pause/            → PAUSED (BY_PERSON), next_run_at kept stale
      │      └── resume/    → ACTIVE, counter reset to 0, next_run_at recomputed
      │
      ├── 3 failed runs     → PAUSED (CONSECUTIVE_FAILURES) + audit WARNING
      │
      ├── owner deactivated → PAUSED (OWNER_INACTIVE) at dispatch time + audit
      │
      └── no future date    → FINISHED   (ONCE that fired, or past ends_on)
                                 └── resume/ refuses: "change the dates instead"
```

A paused schedule deliberately keeps its stale `next_run_at` so resuming can
recompute it (`services.py:598-600`). Resume resets `consecutive_failures` -
without it a schedule paused by three failures would pause again on its very next
failure, however long it had run cleanly in between (`services.py:953-961`).

**A run finishing rolls its schedule forward** (`_advance_schedule`,
`services.py:580-605`), and it is kept out of the two finalisers so both terminal
paths share one rule and a run triggered by hand never touches a schedule it did
not come from:

```
succeeded → register_success()
failed    → register_failure(detail); if now PAUSED → audit EXPORT_SCHEDULE_PAUSED
both      → last_run = run; if still ACTIVE → reschedule()
```

**The dispatcher** (`dispatch_due_schedules`, `services.py:867-935`) runs every
five minutes (`apps/celery.py:77-80`) and does four things nothing else does:

1. A window missed by more than the grace period is **skipped and rolled
   forward**, with a log line, not caught up.
2. A schedule whose owner is no longer ACTIVE **pauses instead of running as
   them**, with an audit event.
3. A tenant at its concurrency cap is left alone with `next_run_at` intact, so
   the next tick retries rather than losing the window (`deferred`).
4. Every started run is attributed to the definition's owner.

It returns `{started, skipped, paused, deferred}`.

## 5. Derivations

All of it lives in `scheduling.py`, and all of it works in the schedule's own
zone, converting to UTC only at the last step (`scheduling.py:77-84`).

| Output | Rule | Where |
|---|---|---|
| `_zone(name)` | the named zone, falling back to `Africa/Lagos` for a zone this machine does not know | `scheduling.py:30-34` |
| `_local(date, time, zone)` | wall-clock time folded onto the date, `fold=0` | `scheduling.py:38-44` |
| `_clamp_day(y, m, d)` | the 31st of a 30-day month means its **last day**, not a skipped occurrence | `scheduling.py:48-51` |
| `ONCE` | `starts_on` only; `None` afterwards | `scheduling.py:86-87` |
| `DAILY` | today if its time has not passed, else tomorrow | `scheduling.py:89-91` |
| `WEEKLY` | the next `day` weekday within 15 days; defaults to `starts_on.weekday()` | `scheduling.py:93-105` |
| `MONTHLY` | `day` (clamped) each month, up to 13 candidates ahead | `scheduling.py:107-125` |
| `QUARTERLY` | the same, striding 3, **keeping the start month's phase** (Jan/Apr/Jul/Oct for a January start) | `scheduling.py:111-115` |
| `should_run_missed` | `now - due_at <= 6h` | `scheduling.py:131-136` |
| `describe` | the sentence the editor reads back | `scheduling.py:139-175` |

Two properties are worth stating because they are what the tests actually pin:

**A clock change keeps the local time fixed.** The occurrence is built as a naive
`datetime.combine` stamped with the zone and only then converted to UTC
(`scheduling.py:44`, `scheduling.py:83-84`), so the UTC instant moves and the
wall clock does not. Test: `tests.py:1936`.

**`next_occurrence` is strictly after `after`.** `_emit` returns `None` for a
moment at or before `after_local` (`scheduling.py:80-82`), which is what stops a
schedule firing twice in the same window when the dispatcher runs at 03:00:00
and again at 03:05:00.

`describe` also appends the current state: "Currently paused: <detail>" or "This
schedule has finished." (`scheduling.py:171-174`), so a schedule explains itself
without the reader interpreting three separate fields.

## 6. What scheduling writes

| Action | Rows | Audit |
|---|---|---|
| `POST /schedules/` | one `ExportSchedule`, then `reschedule()` | `EXPORT_SCHEDULE_CREATED` with recurrence, timezone and `next_run_at` (`views.py:989-998`) |
| `PATCH /schedules/<pk>/` | the schedule, then `reschedule()` | **none** - see §8 |
| `DELETE /schedules/<pk>/` | the row | **none** - see §8 |
| `pause/` | state, reason, detail | `EXPORT_SCHEDULE_PAUSED` with reason and detail (`services.py:944`) |
| `resume/` | state, reason cleared, counter zeroed, `next_run_at` | `EXPORT_SCHEDULE_RESUMED` (`services.py:965`) |
| Auto-pause after 3 failures | state, reason, detail, counter | `EXPORT_SCHEDULE_PAUSED`, WARNING, with the run reference (`services.py:592`) |
| Auto-pause, inactive owner | state, reason, detail | `EXPORT_SCHEDULE_PAUSED`, WARNING (`services.py:905`) |
| Dispatch | one `ExportRun` per due schedule (via `trigger_run`) | `EXPORT_REQUESTED` per run |

Deleting a schedule leaves the export and its files untouched, and the response
says so (`views.py:1033-1035`). Test: `tests.py:2232`.

## 7. Worked example

"Send me July's overdue invoices at 03:00 on the 1st of every month."

```
POST /v1/exports/schedules/
{"definition": 41, "recurrence": "MONTHLY", "day": 1, "at_time": "03:00",
 "timezone_name": "Africa/Lagos", "starts_on": "2026-08-01"}

→ 201
{"id": 7, "state": "ACTIVE", "next_run_at": "2026-09-01T02:00:00Z",
 "consecutive_failures": 0, "last_run": null,
 "reads_as": "runs on day 1 of every month at 03:00 (Africa/Lagos), starting 01 Aug 2026, with no end date. A clock change keeps the local time fixed."}
```

03:00 Lagos is 02:00 UTC, and it stays 03:00 Lagos whatever the offset does.

At 03:00 on 1 September, the five-minute dispatcher tick sees it:

1. `should_run_missed(02:00Z)` - two minutes late, well inside six hours → run.
2. The owner is ACTIVE.
3. `trigger_run(definition=41, actor=owner, trigger=SCHEDULED, schedule=7)`.
4. `schedule.refresh_from_db()`, still ACTIVE, so `reschedule(after=now)` →
   1 October.

Now suppose the export breaks - somebody removed a column from the dataset:

- **1 Oct**: run FAILED (`FILTER_INVALID`), `consecutive_failures = 1`, rolled to
  1 Nov, owner notified.
- **1 Nov**: 2.
- **1 Dec**: 3 → `state = PAUSED`, `pause_reason = CONSECUTIVE_FAILURES`,
  `pause_detail` = the run's user-safe message, one WARNING audit event, and
  `next_run_at` left stale at 1 December.

The Schedules list now reads "Currently paused: This export filters on
'due_bucket', which no longer exists on the Customer invoices dataset." Somebody
fixes the definition and calls `resume/`: state ACTIVE, counter back to 0,
`next_run_at` recomputed to 1 January.

Had the schedule instead been a `ONCE` on 1 September, step 4 would have found no
future occurrence and set **FINISHED** - and `resume/` on it answers "This
schedule has finished - its last occurrence is in the past. Change the dates
instead of resuming it." (`views.py:1046-1054`). Test: `tests.py:2143`.

## 8. Gotchas / known limitations

Full detail in `error/exports/export_code_issues.md`. From this slice:

| # | In one line |
|---|---|
| 2 | Archiving an export does not pause or stop its schedules - it keeps producing a file every night, invisibly |
| 4 | A duplicate run reference raises inside the dispatch loop, which catches only `ExportServiceError`, so the whole tick dies and every other due schedule is skipped |
| 6 | `?definition=` on the schedules list is a raw string on an integer column |
| 10 | `skip_when_empty` is stored, published and read by nothing |
| 14 | The dispatcher takes no lock and scheduled runs carry no idempotency key, so two beat workers double-fire the same window |

Limitations rather than defects:

- **Editing or deleting a schedule is not audited.** Create, pause and resume
  each write an event (`views.py:988`, `services.py:944, 965`); PATCH
  (`views.py:1018-1027`) and DELETE (`views.py:1029-1035`) write none. Changing
  a nightly export's time, or removing the schedule entirely, leaves no trail -
  which is the same argument that made state read-only on PATCH in the first
  place.
- **One time per day, one zone per schedule.** No "twice daily", no "every
  weekday", no business-day calendar. `WEEKLY` with a single `day` is the
  narrowest weekday control there is.
- **The grace period is global.** Six hours suits a nightly file; it is a long
  time for an hourly one, and there are no hourly ones.
- **`reschedule` is called after `refresh_from_db`, not inside a lock**
  (`services.py:930-934`), so it is the second half of the race in
  `export_code_issues` §14.
- **A schedule survives its definition becoming a draft.** Creation refuses a
  draft (`views.py:976-981`); nothing re-checks afterwards, and `trigger_run`'s
  draft guard turns it into an `ExportServiceError` that the dispatch loop counts
  as `deferred` - the same bucket as "at the concurrency cap", so a permanently
  broken schedule is indistinguishable from a busy one in the summary.

## 9. Permissions & tenant isolation

Keys: `exports.schedule.view`, `.schedule.create`, `.schedule.manage`
(`constants.py:329-331`), the last seeded SENSITIVE
(`seed_exports_permissions.py:49-50`).

**Visibility follows the definition.** `get_schedule` (`views.py:164-176`) scopes
by `definition_id__in=visible_definitions()`, so a schedule on a definition you
cannot see is a 404 - including one in another tenant. Test: `tests.py:2074`.
`ScheduleListView` uses the same set (`views.py:955-959`).

**Write requires ownership of the definition**, not merely sight of the schedule:
`get_schedule(for_write=True)` delegates to
`get_definition(schedule.definition_id, for_write=True)` (`views.py:172-176`).
Changing how someone else's export behaves unattended needs the same ownership
rule as changing the export itself. Test: `tests.py:2067`.

**Runs execute as the owner, always.** `dispatch_due_schedules` passes
`actor=definition.owner` (`services.py:920-923`), never the creator of the
schedule, and pauses rather than running when that owner is deactivated
(`services.py:893-912`). Test: `tests.py:2193`.

## 10. Code map

| File | What lives there |
|---|---|
| `models.py:211` | `ExportSchedule`; `reschedule` :276, `register_failure` :292, `register_success` :308 |
| `constants.py:152-185` | `Recurrence`, `ScheduleState`, `PauseReason`, `MAX_CONSECUTIVE_FAILURES`, `MISSED_WINDOW_GRACE_HOURS` |
| `scheduling.py:26-57` | `DEFAULT_TIMEZONE`, `_zone`, `_local`, `_clamp_day`, `_add_months` |
| `scheduling.py:60` | `next_occurrence` - the whole recurrence calculation |
| `scheduling.py:131` | `should_run_missed` |
| `scheduling.py:139` | `describe` - the sentence the editor reads back |
| `services.py:580` | `_advance_schedule` - the one rule both terminal paths share |
| `services.py:867` | `dispatch_due_schedules` |
| `services.py:937, 953` | `pause_schedule`, `resume_schedule` |
| `tasks.py:86` | `dispatch_due_schedules_task` |
| `apps/celery.py:77-80` | The five-minute beat entry |
| `views.py:949-1070` | The four schedule views |
| `serializers.py:505` | `ExportScheduleSerializer`, read-only fields at :526 |

## 11. Test coverage & gaps

Covered - the occurrence maths is tested harder than anything else in the app:

- Daily rolls to tomorrow once today's time has passed (`tests.py:1912`);
  monthly clamps a 31st onto a short month (`tests.py:1924`); **local time
  survives a clock change** (`tests.py:1936`); quarterly keeps the start month's
  phase (`tests.py:1956`); weekly lands on the named weekday (`tests.py:1965`);
  `ONCE` has no second occurrence (`tests.py:1974`); an end date closes the
  series (`tests.py:1982`); an unknown timezone falls back rather than stopping
  the schedule (`tests.py:1990`); a missed window runs inside the grace period
  only (`tests.py:1994`).
- API: creation reads back in plain language (`tests.py:2029`), a draft cannot be
  scheduled (`tests.py:2041`), an unknown timezone is rejected at the edge
  (`tests.py:2050`), end-before-start rejected (`tests.py:2058`), scheduling
  someone else's export is refused (`tests.py:2067`), another tenant's schedule
  is 404 (`tests.py:2074`), **state cannot be set through PATCH**
  (`tests.py:2081`).
- Failure handling: three consecutive failures pause (`tests.py:2094`), a good
  run clears the counter (`tests.py:2104`), a failing scheduled run advances the
  schedule (`tests.py:2110`), resume clears and recomputes (`tests.py:2129`), a
  finished schedule cannot be resumed (`tests.py:2143`), pausing is audited
  (`tests.py:2154`).
- Dispatcher: starts a due schedule and moves it on (`tests.py:2167`), ignores
  one not yet due (`tests.py:2180`), skips a window missed beyond the grace
  period (`tests.py:2184`), pauses for an inactive owner (`tests.py:2193`), keeps
  the window for the next tick at the cap (`tests.py:2205`), never dispatches a
  paused schedule (`tests.py:2221`), the task returns its summary
  (`tests.py:2226`), deleting a schedule leaves files alone (`tests.py:2232`).

Not covered:

- **`skip_when_empty`** - no test, because there is nothing to test
  (`export_code_issues` §10).
- **An archived definition's schedule** (`export_code_issues` §2). Wanted: archive
  a definition, run the dispatcher, assert no run was started.
- **Concurrent dispatch** (`export_code_issues` §14) - every dispatcher test calls
  the function once.
- **A schedule whose definition became a draft after creation**, and the fact
  that the dispatcher reports it as `deferred`.
- **PATCH and DELETE audit** - nothing asserts an event, and nothing writes one.
- **`?state=` and `?definition=` list filters.**
