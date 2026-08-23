# ticket_lifecycle

The ticket itself: how one is filed, numbered, edited, assigned, moved through
its statuses, listed and counted. The conversation on it (comments, files, the
per-ticket audit trail) is `ticket_conversation_attachments`; who may see any of
it is `ticket_visibility_permissions`; what leaves the module (notifications,
exports, the context allowlist) is `ticket_context_integrations`.

Routes are mounted at `/v1/support/` (`apps/urls.py:38`), from
`vs_tickets/urls.py`: a DRF router on `tickets/` plus a single `dashboard/`
view.

---

## 1. What it is (and what it is NOT)

- **It is a support desk, not a helpdesk per school.** One `Ticket` table serves
  every tenant, and the people who work it sit on the platform tenant. A school
  raises a ticket; CX answers it. `visible_tickets_qs` gives platform support
  the one deliberate span over all tenants in this repo
  (`services/visibility.py:92-95`).
- **Filing a ticket needs no permission key.** `RBAC_ACTION_KEYS`
  (`views.py:62-67`) names only `assign`, `transition`, `audit` and
  `eligible_assignees`. Every other action, `create` included, carries no key,
  and `HasRBACPermission` passes a view that declares none
  (`vs_rbac/permissions.py:304-347`). Any authenticated active account can file.
  That is deliberate: escalation must not depend on a grant somebody forgot to
  give you.
- **A ticket is not a workflow instance.** It has its own five-state graph in
  `VALID_STATUS_TRANSITIONS` (`constants.py:85`) and no connection to
  `vs_workflow`. There is no approval, no stage, no escalation ladder.
- **Nothing is ever deleted.** `destroy` raises `PermissionDenied`
  (`views.py:179-180`), so `DELETE /v1/support/tickets/<pk>/` is a `403` for
  everybody, super admin included. There is no archive and no soft-delete flag.
- **A ticket has no SLA.** There is no due date, no first-response clock, no
  breach state. `resolved_at` and `closed_at` are stamps, not targets.
- **`Ticket.tenant` is who raised it, not who is working it.** A CX agent
  working a Bright Star ticket asserts `?tenant=codex`; the row still says
  Bright Star. Every downstream consumer (audit, exports, notifications) reads
  the ticket's tenant rather than the request's - see §6.
- **The list is the only paginated surface in the module.** It inherits
  `XVSPagination` from `DEFAULT_PAGINATION_CLASS`
  (`apps/settings/base.py:66-67`). The dashboard is one aggregate row; comments,
  attachments and the audit trail are not paginated at all (see
  `ticket_conversation_attachments` §8).

## 2. Domain model

Four models, all in `models.py`, plus one legacy table kept only so a migration
would not destroy data.

### `Ticket` (`models.py:48`)

| Field | Notes |
|---|---|
| `ticket_number` | `unique`, `editable=False`, 100 chars. Allocated on first save (§5) |
| `title` (220), `description` (unbounded text) | The two free-text fields. `description` has no length ceiling |
| `category`, `priority`, `status`, `source` | Enums from `constants.py:5,15,23,47`, each `db_index=True` |
| `context` | `JSONField`, allowlisted on write and on read - `ticket_context_integrations` §2 |
| `requester` | `PROTECT`. A user who has ever raised a ticket cannot be deleted |
| `tenant` | `PROTECT`. Set from `requester.tenant` if absent (`models.py:136-137`) |
| `assignee` | `SET_NULL`, nullable. Must be support-capable (§4) |
| `branch` | `PROTECT`, nullable. Captured at creation, never used again (§8) |
| `resolved_at`, `closed_at` | Stamped and cleared by transitions (§4) |
| `objects` / `all_objects` | `TenantAwareManager` / plain. Every view path uses `all_objects` |

Six indexes (`models.py:117-124`): `(status, priority)`, `(requester, status)`,
`(assignee, status)`, `(tenant, status)`, `(category, created_at)`,
`(created_at)`. Default ordering is `-created_at`.

`clean()` (`models.py:126-133`) holds three invariants: the requester belongs to
the ticket's tenant, the branch belongs to the ticket's tenant, and an
`ASSIGNED` ticket has an assignee. It runs on update and on assign
(`full_clean()` at `services/tickets.py:79,113,148`) and **not** on create - see
§8.

`school` / `school_id` (`models.py:142-148`) are convenience properties reading
`tenant.school_profile`. They are the module's one piece of school vocabulary
and are discussed in `ticket_code_issues.md` §15.

### `TicketComment`, `TicketSubscription`, `TicketAttachment`, `TicketAuditLog`

Covered in `ticket_conversation_attachments` §2. All four hang off `Ticket` with
`CASCADE`, so the "nothing is ever deleted" rule is enforced by the absence of a
delete path rather than by the database.

### `TicketSequence` (`models.py:27`)

Dead. The docstring says so: numbering moved to
`vs_tenants.TenantDocumentSequence` and the table is retained only to avoid a
data-destructive migration. Nothing reads or writes it.

## 3. Endpoint map

`?tenant=<slug>` is required on every route: no view here sets
`tenant_param_required = False` (`vs_rbac/authentication.py:128-131`). No view
sets `platform_cross_tenant_param` either, so **a CX agent asserts their own
tenant (`?tenant=codex`) even when working a school's ticket** - the cross-tenant
span comes from the queryset, not from the parameter.

Permissions on every route: `IsAuthenticatedAndActive & HasTicketRBACPermission`
(`permissions.py:28`).

| Method + path | Key | Body / query | Response |
|---|---|---|---|
| `POST /tickets/` | none | `TicketCreateSerializer` (`serializers.py:176`) | `201` + `TicketDetailSerializer` |
| `GET /tickets/` | none | the ten filters below | Paginated `TicketSerializer` |
| `GET /tickets/<pk>/` | none | - | `TicketDetailSerializer`, or `404` |
| `PUT` / `PATCH /tickets/<pk>/` | none | `title`, `description`, `category`, `priority` | `TicketDetailSerializer` |
| `DELETE /tickets/<pk>/` | - | - | always `403` |
| `POST /tickets/<pk>/assign/` | `tickets.ticket.assign` | `{"assignee_id": <int|null>}` | `TicketDetailSerializer` |
| `GET /tickets/<pk>/eligible-assignees/` | `tickets.ticket.assign` | - | `TicketUserSerializer[]` |
| `POST /tickets/<pk>/transition/` | `tickets.ticket.manage` | `{"status": "<TicketStatus>"}` | `TicketDetailSerializer` |
| `GET /dashboard/` | none | - | `TicketDashboardSerializer` |

The comment, attachment, download and audit routes are in
`ticket_conversation_attachments` §3.

### List filters (`views.py:89-125`)

| Param | Effect |
|---|---|
| `status` | exact match |
| `state=active` | `status__in` `ACTIVE_TICKET_STATUSES` (`constants.py:39`) |
| `priority`, `category` | exact match |
| `assignee` | `me` resolves to the caller; anything else is an id |
| `requester` | exact id |
| `school` | joins `tenant__school_profile__id` - legacy, and a school-only path in an engine app |
| `created_from`, `created_to` | `created_at__date__gte` / `__lte` |
| `q` | `icontains` over `title`, `description`, `ticket_number` |

Filtering happens **after** visibility scoping (`views.py:85`, then 89-125), so
no filter can be used to discover a ticket the caller may not see. The `q` search
runs on the queryset rather than the page, so the pagination totals describe the
searched set.

`state=active` exists because the dashboard cards link into the list, and a card
must land on exactly the rows it counted (`views.py:92-96`).

### Serializer field sets

| Serializer | Fields |
|---|---|
| `TicketSerializer` (`serializers.py:58`) | `id`, `ticket_number`, `title`, `description`, `category`, `priority`, `status`, `source`, `context`, `requester`, `assignee`, `tenant` (slug), `branch`, `branch_name`, `resolved_at`, `closed_at`, `comments_count`, `attachments_count`, `created_at`, `updated_at` |
| `TicketDetailSerializer` | the above plus `comments`, `attachments`, `capabilities`, `is_following` |
| `TicketUserSerializer` (`serializers.py:15`) | `id`, `name`, `email`, `tenant_kind`, `role` |
| `TicketDashboardSerializer` (`serializers.py:241`) | `total`, `by_status`, `by_priority`, `by_category`, `assigned_to_me`, `requested_by_me` |

`tenant_kind` rather than a persona column is the point: on a ticket the
distinction that matters is "support desk" versus "the tenant who raised it",
and that is the tenant's kind.

## 4. Lifecycle / state machine

```text
                    assign(someone)
        OPEN ─────────────────────────────► ASSIGNED
          │  ◄───────────────────────────      │
          │        assign(null)                │
          │                                    │
          ├──────────────► IN_PROGRESS ◄───────┤
          │                  │   ▲   ▲
          │                  ▼   │   │
          ├──────────────► RESOLVED │   (reopen)
          │                  │      │
          ▼                  ▼      │
        CLOSED ◄─────────────┴──────┘
          │
          └──── IN_PROGRESS  (reopen; clears closed_at)
```

Encoded once, in `VALID_STATUS_TRANSITIONS` (`constants.py:85`). What the graph
says, in words:

- **Any live state can jump straight to `RESOLVED` or `CLOSED`.** A ticket does
  not have to be assigned or worked first.
- **`ASSIGNED` is not reachable by transition.** It is reached only by assigning
  somebody to an `OPEN` ticket (`services/tickets.py:104-106`) and left only by
  clearing the assignee, which returns it to `OPEN` (107-109). That pairing is
  what keeps `clean()`'s "assigned tickets require an assignee" true.
- **Reopening is `→ IN_PROGRESS`,** from either `RESOLVED` or `CLOSED`. There is
  no path back to `OPEN` from a finished state.
- **Repeating the current status is a no-op**, not an error, and writes no audit
  row (`services/tickets.py:133-135`).

Timestamps (`services/tickets.py:141-148`): `RESOLVED` stamps `resolved_at`;
`CLOSED` stamps `closed_at`; `IN_PROGRESS` clears whichever of the two the
previous state had set. A resolve-then-close keeps both stamps; a reopen from
closed keeps `resolved_at` and clears `closed_at` only.

`ACTIVE_TICKET_STATUSES` (`constants.py:39`) is `OPEN, ASSIGNED, IN_PROGRESS` and
is the single definition of "still on somebody's plate". Its docstring is worth
reading before adding another counter: a workload number that counted `RESOLVED`
rows would leave a badge the reader cannot clear by doing the work, and one that
counted `OPEN` alone would drop a ticket the moment somebody picked it up.

## 5. Derivations

- **`ticket_number` is `TK-<tenant_id><YYMMDD><n>`**, allocated on first save
  (`models.py:138-139,150-155`) from the shared tenant counter
  (`vs_tenants/numbering.py`). The counter is per `(tenant, "TK", local date)`,
  protected by a unique constraint and a `select_for_update` row lock, and `n`
  restarts at 1 each day and is not zero-padded.
- **`tenant` falls back to the requester's** (`models.py:136-137`), but the
  service always passes it explicitly (`services/tickets.py:52`), so the fallback
  only fires for code creating tickets directly.
- **`source` is derived, never sent.** `INTERNAL` if the actor is support,
  `CUSTOMER` otherwise (`services/tickets.py:40`). It is read-only on every
  serializer.
- **`branch` is the actor's own** (`services/tickets.py:32-33,38`). The create
  serializer has no `branch` field, so a caller cannot choose one, and a user
  with no branch produces a null branch - which means "the school as a whole",
  not "no branches exist".
- **`comments_count` / `attachments_count` are annotated per row**
  (`views.py:73-88`) and are *visibility-aware*: a caller who cannot see internal
  notes gets counts that exclude internal notes and the files hanging off them.
  Both use `distinct=True`, because two `Count`s over two joins in one query
  otherwise multiply each other.
- **The dashboard is one `aggregate()` call** (`views.py:328-341`) building a
  key per enum value so every status, priority and category appears even at zero.
  `total` and the three breakdowns are whole-population; `assigned_to_me` and
  `requested_by_me` are filtered by `ACTIVE_TICKET_STATUSES`, because they answer
  "what is still on me" rather than "what have I ever touched".
- **Updates are allowlisted twice.** `TicketUpdateSerializer`
  (`serializers.py:184`) accepts four fields, and `update_ticket` intersects
  whatever it is given with `{"title", "description", "category", "priority"}`
  again (`services/tickets.py:70-72`), so a service-layer caller cannot change
  ownership or status through the update path either. An update reduced to
  nothing returns the ticket unchanged and writes no audit row (73-74).

## 6. What writing writes

Every write goes through `services/tickets.py`, inside `transaction.atomic()`,
and every one of them writes **two** audit records through
`record_ticket_audit` (`services/audit.py:13`):

1. a `TicketAuditLog` row - the per-ticket history the UI shows;
2. a platform audit event via `emit_audit_event`, `module_key="SYSTEM"`,
   `entity_type="Ticket"`.

The platform event is stamped `tenant=ticket.tenant`, explicitly, rather than
inheriting the request's tenant (`services/audit.py:27-31`). Without that, a CX
agent asserting `?tenant=codex` while resolving a Bright Star ticket would file
Bright Star's support history under Codex.

Impersonation survives: `resolve_audit_identity` and `add_proxy_audit_metadata`
(`services/audit.py:15-17`) record the real actor behind a proxy session in the
audit metadata.

| Write | Audit action | Notification |
|---|---|---|
| `create_ticket` | `CREATED` (with `after_data`) | `ticket.created` → the support queue |
| `update_ticket` | `UPDATED` (before + after) | none |
| `assign_ticket` | `ASSIGNED` (before + after, `metadata.assignee_id`) | `ticket.assigned` → the new assignee, only when one is set |
| `transition_ticket` | `STATUS_CHANGED` (before + after, old/new status) | `ticket.resolved` / `.closed` / `.reopened` / `.status_changed` |

`snapshot_ticket` (`services/audit.py:61`) is the before/after shape: ids and
scalars only, no serialized user objects.

Notifications are queued on `transaction.on_commit` and swallow their own
exceptions, so a notification failure can never roll back a ticket
(`services/notifications.py:78-82`). Details in
`ticket_context_integrations` §4.

## 7. Worked example

```text
POST /v1/support/tickets/?tenant=bright-star
{"title": "Fee receipt will not print",
 "description": "The print button does nothing on the receipt screen.",
 "category": "BUG", "priority": "HIGH",
 "context": {"product_area": "Finance", "app_version": "2.14.0"}}
```

```json
{ "success": true, "message": "Ticket created successfully.",
  "data": {
    "id": 4471, "ticket_number": "TK-72608213",
    "title": "Fee receipt will not print",
    "category": "BUG", "priority": "HIGH", "status": "OPEN", "source": "CUSTOMER",
    "context": {"product_area": "Finance", "app_version": "2.14.0"},
    "requester": {"id": 903, "name": "Ngozi Bello", "email": "ngozi@brightstar.test",
                  "tenant_kind": "SCHOOL", "role": "Bursar"},
    "assignee": null, "tenant": "bright-star",
    "branch": 12, "branch_name": "Main",
    "resolved_at": null, "closed_at": null,
    "comments_count": 0, "attachments_count": 0,
    "capabilities": {"can_comment": true, "can_attach": true} } }
```

`TK-72608213` reads as tenant 7, 21 August 2026, third ticket that tenant raised
that day.

Then CX picks it up and finishes it:

```text
POST /v1/support/tickets/4471/assign/?tenant=codex   {"assignee_id": 22}
  → status OPEN → ASSIGNED, audit ASSIGNED, ticket.assigned to user 22

POST /v1/support/tickets/4471/transition/?tenant=codex  {"status": "IN_PROGRESS"}
POST /v1/support/tickets/4471/transition/?tenant=codex  {"status": "RESOLVED"}
  → resolved_at stamped, audit STATUS_CHANGED ×2, ticket.resolved to requester
```

A second `{"status": "RESOLVED"}` returns `200` with the ticket unchanged and
writes nothing. `{"status": "OPEN"}` from `RESOLVED` is a `400`: the graph has no
such edge.

## 8. Gotchas / known limitations

Full evidence for each is in **`error/tickets/ticket_code_issues.md`**. The items
belonging to this slice:

- **A school that has not gone live can file a ticket and then cannot read the
  reply.** `pending_tenant_surface = ("create",)` (`views.py:57`) opens exactly
  one action; the ticket's own detail page, its comments and its list are all
  `TenantNotLive` for the same caller (`ticket_code_issues.md` §4).
- **`?assignee=`, `?requester=` and `?school=` are 500s on a non-numeric value**
  (`views.py:101-114`): the `ValueError` from the ORM is not a handled exception
  type (`ticket_code_issues.md` §9). The date filters are fine - Django's
  `ValidationError` becomes a `400`.
- **`Ticket.clean()` never runs on the create path.** `create_ticket` calls
  `Ticket.objects.create` (`services/tickets.py:45`) with no `full_clean()`, so
  the branch-belongs-to-tenant invariant is enforced on update and assign but not
  where tickets are actually made (`ticket_code_issues.md` §7).
- **`branch` is captured and then ignored.** No filter, no column in the list, no
  branch narrowing in visibility - so a branch admin at one site sees every
  branch's tickets, and a school with several branches cannot tell them apart
  (`ticket_code_issues.md` §6).
- **`tickets.report.view` is seeded and never checked.** The dashboard declares
  no key (`views.py:318-321`), so any authenticated account gets it - correctly
  scoped, but the key means nothing (`ticket_code_issues.md` §12).
- **`TicketAssignSerializer` answers a different error for an id that exists**
  (`serializers.py:194-199`): "No such user." for a free id, and a
  support-capability message for a real one, which distinguishes real user ids
  from free ones (`ticket_code_issues.md` §14).
- **`views.assign` re-fetches with a bare `User.objects.get`** (`views.py:190`).
  If the row disappears between validation and fetch the result is a `500`, not a
  `400`.
- **An unassigned `IN_PROGRESS` ticket is possible and notifies nobody.**
  `assign_ticket` only reopens a ticket when the status is exactly `ASSIGNED`
  (`services/tickets.py:107-109`), so clearing the assignee of an in-progress
  ticket leaves it live, ownerless, and silent.
- **`description` and comment bodies have no length ceiling** - `TextField` and
  `CharField()` with no `max_length` (`models.py:52`, `serializers.py:207`).
- **`TicketSerializer.Meta.read_only_fields` omits `title`, `category`,
  `priority` and `branch`** (`serializers.py:82-85`). Harmless today because the
  write paths use their own serializers, but it reads as permission the class
  does not actually give.
- **`TicketSequence` (`models.py:27`) is dead** and still migrated.
- **Justified by design:** `destroy` is a blanket `403` (`views.py:179-180`) -
  the audit trail is the reason, and a support conversation nobody can erase is
  the point.
- **Justified by design:** search runs on the queryset rather than the page
  (`views.py:119-125`), so the totals describe the searched set.

## 9. Permissions & tenant isolation

| Action | Gate |
|---|---|
| create, list, retrieve, update | none - queryset and object scoping only |
| assign, eligible-assignees | `tickets.ticket.assign` |
| transition | `tickets.ticket.manage` |
| dashboard | none |
| destroy | nobody |

`HasTicketRBACPermission` (`permissions.py:11`) passes anybody
`is_support_user()` admits, before the key check. The full account of who that
is, and the three different answers this module gives to that question, is
`ticket_visibility_permissions`.

Isolation for everything in this slice comes from one function,
`visible_tickets_qs` (`services/visibility.py:82`), and its object-level twin
`can_view_ticket` (110). Both are used: the list annotates the queryset, and
`get_object` fetches by pk from `all_objects` and then asks `can_view_ticket`,
raising `NotFound` rather than `PermissionDenied` (`views.py:139-149`) so a
hidden ticket is indistinguishable from a missing one.

The dashboard uses the same queryset (`views.py:323`), which is what stops its
counters from describing rows the caller could not open.

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py:48-158` | `Ticket` - fields, indexes, `clean()`, numbering hook |
| `models.py:27-45` | `TicketSequence` - legacy, unused |
| `constants.py:5-85` | The five enums, `ACTIVE_TICKET_STATUSES`, the permission keys, the transition graph |
| `views.py:47-219` | `TicketViewSet` - queryset, filters, create/update, assign, transition |
| `views.py:318-353` | `TicketDashboardView` |
| `services/tickets.py:37-168` | `create_ticket`, `update_ticket`, `assign_ticket`, `transition_ticket` |
| `services/audit.py` | `record_ticket_audit`, `snapshot_ticket` - the local log and the platform mirror |
| `serializers.py:58-122` | List and detail output |
| `serializers.py:176-204` | Create, update, assign, transition input |
| `permissions.py` | `HasTicketRBACPermission`, `TICKET_PERMISSIONS` |
| `vs_tenants/numbering.py` | `next_tenant_document_number` - the `TK-` counter |
| `core/pagination.py` | `XVSPagination` - the `{pagination, data}` envelope the list returns |

## 11. Test coverage & gaps

- `TicketServiceTests` (`tests.py:124-428`) - creation scopes to the requester's
  tenant and audits; numbers are sequential, unique and per-tenant; assignment
  moves `OPEN → ASSIGNED`; the transition chain stamps `resolved_at` and writes
  two audit rows; an authenticated user with no ticket keys can still file.
- `TicketApiSecurityTests` (`tests.py:430-704`) - cross-tenant retrieve is a
  `404`; a same-tenant peer can neither list nor open another employee's ticket;
  a requester can neither transition nor assign their own ticket; a school
  manager holding `manage` can transition; the assignee picker offers only real
  handlers and rejects anybody else with a `400`.
- Dashboard: `test_dashboard_counts_visible_tickets_only`,
  `..._assigned_to_me_counts_live_work_not_finished_tickets`,
  `..._requested_by_me_counts_live_work_only`, and
  `test_list_state_active_and_assignee_me_match_the_counters` - the cards and the
  list they link to are asserted to agree.
- `TicketBranchTenantGuardTests` (`tests.py:725-786`) - the branch/tenant guard,
  asserted directly on `clean()`.

What the suite does not cover:

1. **The tenant parameter.** Every API test uses `force_authenticate`, which
   skips `TenantJWTAuthentication` entirely, so `request.tenant` is never set and
   the ambient `TenantAwareManager` is never active in a test. If a queryset here
   switched from `all_objects` to `objects`, nothing would fail.
2. **The pending-tenant surface.** Nothing asserts that a PENDING tenant can
   `POST` a ticket, and nothing asserts what happens when the same caller then
   opens it.
3. **`create_ticket` versus `clean()`** - the guard is tested on the model and
   never through the service that creates tickets.
4. **Malformed filter values** (`?assignee=abc`), and every filter except
   `state`, `status` and `assignee=me`.
5. **The empty-list response shape** on `GET /tickets/`, which matters because
   `success_response` coerces `[]` to `{}` (`core/response.py:6-11`). Only the
   empty *comment* list is asserted (`tests.py:634-638`).
6. **`update_ticket`** has no test at all - not the allowlist, not the audit row,
   not the no-op return.
7. **Reopen paths**: `CLOSED → IN_PROGRESS` clearing `closed_at`, and
   `RESOLVED → IN_PROGRESS` clearing `resolved_at`.
8. **Clearing an assignee** - neither the `ASSIGNED → OPEN` return nor the
   ownerless `IN_PROGRESS` ticket it can leave behind.
9. **`DELETE`** - nothing asserts the `403`.
