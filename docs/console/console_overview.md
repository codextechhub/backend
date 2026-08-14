# console_overview

The **landing screen in one request**: school and team counts, the caller's next
tasks, their approval queue, returned submissions, unread notifications, open
tickets, system posture, and a set of "act on this soon" signals. Routes are at
`/v1/admin/dashboard/overview/` and `/v1/admin/dashboard/`.

---

## 1. What it is (and what it is NOT)

- One `GET` that replaces eight dashboard calls. The win is not the sum of their
  costs (they were already issued in parallel) but one authentication, one
  tenant resolution and one permission evaluation instead of eight, over one
  round trip (`overview.py:1-15`).
- **The endpoint itself is gated by nothing but an active account.** Every
  number inside it carries the key of the screen it came from, checked
  section by section in `console_overview` (`views.py:509`; `overview.py:463-500`).
- **A section the caller may not see is omitted, never zeroed.** `0` and "you
  have no access" must not look the same on screen, and the frontend hides the
  card behind the matching key anyway (`overview.py:33-36`).
- Signals apply that rule twice: absent when not permitted **and** absent when
  there is nothing to act on. A healthy signal is silence, not a green card
  (`overview.py:256-263`).

**This is not the source of any of these numbers.** Each section calls the same
service the owning module's own screen calls; two of the endpoints it replaces
serialised an entire dashboard so the screen could read one field, and that is
what this avoids (`overview.py:10-15`).

**`GET /v1/admin/dashboard/` is a stub.** It validates its filters and returns an
empty list (`views.py:457-486`).

## 2. Domain model

None. This app owns no table for this slice; every section reads another
module's models through that module's own service or manager:

| Section | Reads | Through |
|---|---|---|
| `schools` | `vs_schools.School` | conditional aggregate (`overview.py:67-74`) |
| `team` | `vs_user.User` | `user_type=CX_STAFF`, `status=ACTIVE` (`overview.py:77-93`) |
| `tasks` | `vs_todo.Task` | `own_tasks_qs` + `stats_for` (`overview.py:96-122`) |
| `approvals` | `vs_workflow.WorkflowStageApprover` | `my_queue.pending_approval_snapshots` (`overview.py:125-179`) |
| `submissions` | `vs_workflow.WorkflowInstance` | `status="RETURNED"` (`overview.py:182-200`) |
| `notifications` | `vs_notifications.Notification` | unread in-app for the caller (`overview.py:203-214`) |
| `tickets` | `vs_tickets.Ticket` | `visibility.visible_tickets_qs` (`overview.py:217-226`) |
| `health` | `vs_health.MonitoredService`, `Incident` | `overall_posture()` (`overview.py:229-238`) |
| `signals` | finance, procurement, payments, rbac, todo, `core.BackgroundJob` | see §5 (`overview.py:256-460`) |

## 3. Endpoint map

| Method + path | permission key | request body / query | response |
|---|---|---|---|
| `GET /dashboard/overview/` | active account only | `?tenant=` (required) | The assembled sections; see §7 (`views.py:489-515`) |
| `GET /dashboard/` | `platform.dashboard.view` | `q`, `lifecycle_state`, `provisioning_status`, `is_suspended`, `created_after`, `created_before` | `data: {}` - the item list is still a `TODO` (`views.py:457-486`) |

`dashboard/overview/` is declared **ahead of the router** in `urls.py`, because
`dashboard` is a registered basename and a router lookup would otherwise read
`overview` as a detail pk (`urls.py:18-23`).

`DashboardFilterSerializer` is the one thing the stub really does: it rejects
`created_after > created_before` with a `400`, so the eventual implementation
inherits the contract (`serializers.py:145-163`).

## 4. Lifecycle / state machine

There is none - the endpoint is a read. The only ordering that matters is the
assembly order in `console_overview` (`overview.py:463-500`):

```text
schools        if platform.schools.view
team           if platform.team.view
tasks          if user_type == CX_STAFF
approvals      always      (own queue)
submissions    always      (own submissions)
notifications  always      (own unread)
tickets        if support user OR tickets.ticket.view
health         if platform.health.view
signals        per-signal gate, and only when non-empty
```

Permissions are evaluated against `request.user`, which during a live proxy is
the **target**, and against `request.tenant`
(`overview.py:465-466`). Because `get_effective_permissions` returns an empty set
whenever the asserted tenant is not the user's own
(`vs_rbac/evaluator.py:121-122`), and this view does not set
`platform_cross_tenant_param`, the gates always evaluate in the caller's home
tenant.

## 5. Derivations

- **`schools.active`**: `Count("slug", filter=Q(status=ACTIVE))` - the same
  conditional aggregate `SchoolStatsView` uses (`overview.py:67-74`).
- **`team.total`**: active `CX_STAFF` accounts. A platform-kind actor gets the
  platform-wide count; anyone else is narrowed to the asserted tenant
  (`overview.py:87-93`).
- **`tasks`**: `stats_for` counts total / done / in progress / overdue and a
  completion percentage (`vs_todo/services/stats.py:16-35`). The three listed
  items are sorted in Python over the list `stats_for` already materialised -
  overdue first, then `HIGH`/`MEDIUM` priority, then nearest deadline - so the
  panel costs no second query (`overview.py:109-117`).
- **`approvals.pending`**: `len()` of the snapshot list, because the
  already-acted and stale-attempt filters cannot be pushed into SQL
  (`vs_workflow/services/my_queue.py:54-64`). The five listed items come from
  that same list, sorted **oldest activation first** so a lingering decision can
  never hide below the cap while fresh arrivals fill the visible rows
  (`overview.py:138-146`). One `in_bulk` resolves requester and
  on-behalf-of names (`overview.py:148-160`).
- **`approvals.delegated`**: snapshots where `on_behalf_of_id` is set - the
  caller is covering someone else's decision (`overview.py:178`).
- **`submissions`**: the caller's own `WorkflowInstance` rows in `RETURNED`,
  newest first, capped at three (`overview.py:182-200`).
- **`notifications.unread`**: `recipient=caller`, `channel=IN_APP`,
  `is_read=False` - the query behind the bell badge (`overview.py:203-214`).
- **`tickets`**: `open` and `assigned_to_me` counted inside
  `visible_tickets_qs`, which is the full-tenant set for platform support users
  and participant-or-manager only for everyone else
  (`vs_tickets/services/visibility.py:73-93`).
- **`health`**: `label`, `overall` and `active_incidents` from
  `overall_posture()` - service-derived posture only, not the Command Center
  payload (`vs_health/services.py:463-479`).

The caps are constants: three tasks, five approvals, three returned submissions
(`overview.py:54,58-59`). Signals use a 24-hour window and a 30-day contract
horizon (`overview.py:251-253`).

### Signals

Each entry is emitted only if the caller holds the key of the screen it points
at **and** the count is non-zero (`overview.py:256-460`):

| Signal | Key | What it counts |
|---|---|---|
| `fiscal_runway` | `finance.report.view` | Worst non-healthy `fiscal_calendar_runway` across active ledger entities; a missing calendar ranks worst of all (`overview.py:269-293`) |
| `draft_journals` | `finance.journal.view` | `JournalEntry` in `DRAFT` (`overview.py:295-301`) |
| `pos_awaiting_receipt` | `procurement.purchase_order.view` | Issued POs whose `po_receipt_stage` is not `RECEIVED`; drafts and in-approval orders are not commitments (`overview.py:303-330`) |
| `webhook_failures_24h` | `payments.webhook.view` | `WebhookEvent` in `FAILED` in the last 24h (`overview.py:332-340`) |
| `overdue_invoices` | `finance.invoice.view` | Posted invoices past `due_date` and not `PAID` (`overview.py:342-356`) |
| `unallocated_credit` | `finance.payment.view` | Posted receipts where `amount > allocated + refunded` (`overview.py:358-371`) |
| `vendor_invoices_unpaid` | `procurement.vendor_invoice.view` | Posted vendor bills not `PAID` (`overview.py:373-385`) |
| `rfqs_open` | `procurement.rfq.view` | RFQs in `ISSUED` (`overview.py:387-394`) |
| `contracts_expiring` | `procurement.contract.view` | `ACTIVE` contracts ending within 30 days (`overview.py:396-405`) |
| `users_without_roles` | `platform.roles.view` or `school.roles.view` | Active accounts in the caller's tenant with no `ACTIVE` role assignment (`overview.py:407-424`) |
| `team_overdue_tasks` | `user_type == CX_STAFF` | Overdue tasks assigned to the caller's own reporting subtree (`overview.py:426-439`) |
| `jobs_failed_24h` | active account | The caller's own `BackgroundJob` rows in `FAILED` in the last 24h (`overview.py:444-450`) |
| `jobs_succeeded_24h` | active account | Same for `SUCCEEDED` - the counterpart the live queue toasts may have missed (`overview.py:452-458`) |

All the key checks hit the per-request memo on the user instance, so the whole
signal block costs one permission query, not thirteen
(`vs_rbac/evaluator.py:124-136`).

## 6. What posting does to the ledger

Nothing posts and nothing is written. This slice is the only fully read-only
surface in the module: no audit event, no status change, no side effect. That is
worth stating explicitly, because both other slices in `vs_admin_console`
mutate on `GET`.

One indirect write does happen off the approvals path:
`pending_approval_snapshots` first calls `parking.repair_workflows(tenant=...)`,
which restores parked work for the tenant being read
(`vs_workflow/services/my_queue.py:36-40`). On healthy data it is one indexed
query that matches nothing.

## 7. Worked example

```text
GET /v1/admin/dashboard/overview/?tenant=corona
```

For a school bursar holding `finance.invoice.view` and nothing else:

```json
{ "success": true, "message": "Overview retrieved successfully.",
  "data": {
    "approvals": { "pending": 2, "delegated": 1, "items": [
      { "id": "9f1c…", "document_type": "purchase_order",
        "document_object_id": 3312, "stage_label": "Bursar review",
        "awaiting_since": "2026-08-12T08:00:00Z",
        "requested_by_name": "Ngozi Eze", "on_behalf_of_name": "Tunde Bello" }
    ]},
    "submissions": { "returned": 0, "items": [] },
    "notifications": { "unread": 4 },
    "signals": { "overdue_invoices": { "count": 17 } }
  } }
```

`schools`, `team`, `tasks`, `tickets` and `health` are **absent**, not zero - the
bursar holds none of their keys. `signals` carries one entry: the other twelve
are either not permitted or quiet.

A caller with no keys, no queue and nothing outstanding gets `"data": {}`,
because `success_response` coerces an empty dict to `{}` on the way out
(`core/response.py:6-11`).

## 8. Gotchas / known limitations

- **Nine of the thirteen signals count every tenant's documents.** The finance,
  procurement and payments models these signals read are **entity-scoped, not
  tenant-scoped**: `FinanceDocument` has an `entity` FK and no `tenant` field
  and uses the default manager (`vs_finance/models/core.py:255-289`), and
  `LedgerEntity.objects` is a plain `Manager` too
  (`vs_finance/models/core.py:49-62,142`). `TenantAwareManager` therefore never
  engages. Every one of `fiscal_runway`, `draft_journals`,
  `pos_awaiting_receipt`, `webhook_failures_24h`, `overdue_invoices`,
  `unallocated_credit`, `vendor_invoices_unpaid`, `rfqs_open` and
  `contracts_expiring` counts across the whole platform
  (`overview.py:269-405`). The modules' own screens do not: `resolve_entity`
  pins every finance read to an entity inside `request.tenant` and returns
  `NotFound` otherwise (`vs_finance/views.py:56-81`). So a school finance
  officer who legitimately holds `finance.report.view` sees another tenant's
  document counts, and `fiscal_runway` returns the offending **entity name** in
  clear (`overview.py:284-290`). This is the item to fix first in this slice,
  and the fix belongs in `_signals`: scope each queryset to
  `LedgerEntity.objects.filter(tenant=tenant)` the way the finance views do.
- **`_tasks` loads every task the caller owns into Python to display three.**
  `own_tasks_qs(user)` is unfiltered by status or date, the whole list is
  materialised for `stats_for`, and then sliced to three
  (`overview.py:106-117`; `vs_todo/services/stats.py:38-40`). It is correct and
  it is one query, but it is an unbounded queryset on the landing screen of
  someone who has been assigned tasks for two years.
- **`team` is a platform number behind a key a school role can hold.** For a
  platform-kind actor `_team` counts `CX_STAFF` across all tenants
  (`overview.py:87-93`). For anyone else it scopes to the asserted tenant - but
  `CX_STAFF` accounts only exist in the platform tenant, so a school role
  granted `platform.team.view` gets a card that always reads `0`. Either the
  section should not be offered outside the platform tenant, or the key should
  not be grantable there.
- **The approvals sort can raise on data the routing engine cannot currently
  produce.** The key is `(activated_at is None, activated_at)`
  (`overview.py:140-146`); two snapshots that both have `activated_at = None`
  compare `None < None` and raise `TypeError`. Every path that sets a stage to
  `ACTIVE` also stamps `activated_at`
  (`vs_workflow/services/routing.py:201-207`), so this is unreachable through
  the engine today - but it is reachable from fixtures, seeds, or any future
  path that activates a stage without the timestamp, and the failure mode is a
  500 on the landing screen rather than a missing card.
- **`APPROVAL_ITEMS_LIMIT` and `RETURNED_ITEMS_LIMIT` are defined twice**, with
  the same comment block copied above each pair (`overview.py:57-64`). Harmless
  - the values are identical - but the second pair silently wins, so an edit to
  the first has no effect.
- **`GET /dashboard/` is a stub that answers `200`.** It validates filters and
  returns `data: {}` (`views.py:474-486`), which is indistinguishable from "this
  platform has no schools". It has been carrying a `TODO` since it was written
  and no test covers it.
- **`platform.dashboard.view` guards nothing.** It is seeded
  (`core/management/commands/seed_platform_permissions.py:143-148`) and gates
  only the stub; the real landing screen deliberately needs no key. Worth
  keeping in mind when someone reads the permission catalogue and assumes the
  overview is behind it.
- **Justified by design:** omitting a section rather than returning zero. The
  test suite guards it as the first group, precisely because a zero would read
  as real data (`tests_overview.py:1-10,81-135`).
- **Justified by design:** `approvals` and `submissions` need no key. They
  return the caller's own rows only, matching the endpoints they replace
  (`overview.py:481-485`).
- **Justified by design:** the `tasks` section is `CX_STAFF`-only rather than
  key-gated, because `vs_todo` itself is an `IsVisionStaff` surface
  (`overview.py:476-479`).

## 9. Permissions & tenant isolation

| Section | Gate | Scope of the number |
|---|---|---|
| Endpoint | `IsAuthenticatedAndActive` | n/a |
| `schools` | `platform.schools.view` | Platform-wide (correct - platform-only key) |
| `team` | `platform.team.view` | Platform-wide for platform actors, asserted tenant otherwise |
| `tasks` | `user_type == CX_STAFF` | Own tasks |
| `approvals` | none | Own queue, tenant-filtered |
| `submissions` | none | Own submissions, tenant-filtered |
| `notifications` | none | Own unread |
| `tickets` | support user **or** `tickets.ticket.view` | Own visibility set |
| `health` | `platform.health.view` | Platform-wide (correct - platform infrastructure) |
| `signals` (finance/procurement/payments) | each module's `view` key | **Platform-wide - see §8** |
| `signals.users_without_roles` | `platform.roles.view` / `school.roles.view` | Caller's tenant |
| `signals.team_overdue_tasks` | `user_type == CX_STAFF` | Own reporting subtree |
| `signals.jobs_*_24h` | none | Own jobs |

Isolation comes from three different mechanisms and one gap: the tenant-aware
manager (`Notification`, `User`), an explicit `tenant=` filter
(`_team`, `_submissions`, `users_without_roles`), an explicit `user=` filter
(tasks, approvals, jobs) - and, for the nine module signals, nothing.

## 10. Code map

| File | Responsibility |
|---|---|
| `overview.py` | Every section, its gate, and the signal block. The module docstring is the contract |
| `views.py:489-515` | `ConsoleOverviewView` - the one `GET`, one `success_response` |
| `views.py:457-486` | `DashboardViewSet` - the filter-validating stub |
| `serializers.py:123-163` | `SchoolDashboardItemSerializer` and `DashboardFilterSerializer` (stub only) |
| `urls.py:18-23` | The route-ordering workaround that keeps `overview` off the router |
| `vs_rbac/evaluator.py` | `has_permission` and the per-request memo every gate rides on |

## 11. Test coverage & gaps

`tests_overview.py` is organised security-first, deliberately
(`tests_overview.py:1-10`). Six groups, all driving the real JWT layer
(`tests_overview.py:66-77`):

- `OverviewPermissionTests` (`tests_overview.py:81`) - the matrix that matters:
  anonymous refused, an ungranted user getting **none** of the four gated
  sections, each key unlocking only its own section, one key not unlocking the
  others, the three own-data sections needing no key, and `tasks` being
  `CX_STAFF`-only.
- `OverviewSectionTests` (`tests_overview.py:137`) - per-section arithmetic:
  active schools only, active CX staff, task stats and the three listed items,
  own tasks only, own unread in-app only, posture rather than the whole
  Command Center, and the empty state being zeros rather than missing keys.
- `OverviewWorklistTests` (`tests_overview.py:222`) - what an approval row
  renders, own queue only, the cap applying to items but **not** to the count,
  and returned submissions being own/newest-first/capped.
- `OverviewSignalTests` / `OverviewExpandedSignalTests`
  (`tests_overview.py:328,439`) - silence when quiet, own recent job failures
  only, and per-signal gating and arithmetic for webhooks, fiscal runway, draft
  journals, open POs, overdue invoices, unallocated credit, unpaid vendor
  invoices, open RFQs, expiring contracts, roleless users and the manager
  subtree.
- `OverviewTenantIsolationTests` (`tests_overview.py:421`) - the one section
  that spans tenants: a school actor counts no platform staff, a platform actor
  keeps the platform-wide count.
- `OverviewDelegationAndExportTests` (`tests_overview.py:618`) - the delegated
  count and its per-item flag, and own recent completions.

Three gaps:

1. **No test builds a second tenant's finance data and asserts a signal
   excludes it.** `test_overdue_invoices_counts_posted_unpaid_past_due`
   (`tests_overview.py:453`) and its eight siblings all build rows in one
   tenant, which is exactly why the leak in §8 reads as passing coverage. The
   `OverviewTenantIsolationTests` pattern is the one to extend.
2. **`GET /dashboard/` has no test at all** - not its permission key, not its
   filter validation, not its empty-response shape.
3. **Nothing covers the `403`/`404` edges of the endpoint itself**: a missing
   `?tenant=`, an unknown slug, or a school actor asserting a foreign slug.
   Those are enforced in `TenantJWTAuthentication`, not here, but this is the
   surface every signed-in user hits first.
