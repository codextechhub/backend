# notification_feed_history

The two read surfaces over `Notification`: the signed-in user's **in-app feed**
(their own rows, with read state) and the admin **delivery history log** (the
tenant's rows, with failure detail). Routes are mounted at `/v1/notify/`
(`apps/urls.py:29`).

Feed: `/`, `/<uuid>/`, `/unread-count/`, `/mark-read/`, `/mark-all-read/`,
`/acknowledge-route/`.
History: `/history/`, `/history/<uuid>/`.

---

## 1. What it is (and what it is NOT)

- **The feed is the in-app channel.** `NotificationViewSet.get_queryset`
  filters `recipient = request.user` and `channel = IN_APP`
  (`views.py:133-141`). Email rows exist in the same table and never appear
  here.
- **Nothing in the feed can be created or deleted.** The only writes are read
  state: `is_read` and `read_at` (`views.py:212-282`).
- **The history log is per-tenant, for everyone.** `get_queryset` filters
  `tenant = request.tenant` for every caller including CX staff, using
  `all_objects` so the `include_global` manager change cannot leak tenant-NULL
  rows in (`views.py:320-325`). There is no `?school=` parameter and no
  cross-tenant path: asserting another tenant's slug is refused with `404` by
  the auth layer, and this view does not opt in via
  `platform_cross_tenant_param`.
- **History is not an audit trail.** It shows what was dispatched, not who
  triggered it. There is no actor column on `Notification` at all.
- **`acknowledge-route` is not a mark-read shortcut.** It maps a frontend path
  to the events that point at it and marks only those read
  (`services/routing.py:52-74`), so opening a ticket clears that ticket's
  notifications and nothing else.
- **Neither surface exposes `metadata`.** It is internal-only and every
  serializer in this module deliberately omits it
  (`serializers.py:17-18,84`).

## 2. Domain model

No model is owned here. Both surfaces read `Notification` (`models.py:414`);
see `notification_dispatch_engine` §2 for the full field list. What matters for
reading:

| Field | Read by |
|---|---|
| `recipient`, `channel`, `is_read`, `read_at` | the feed, and the covering index `(recipient, channel, is_read, -created_at)` (`models.py:553`) |
| `tenant` | the history scope filter and both tenant indexes (`models.py:555-556`) |
| `subject`, `body` | both, already rendered and frozen at dispatch |
| `metadata` | never serialized; read only by `notification_action_url` to build a destination |
| `status`, `retry_count`, `failure_reason` | history only |
| `unregistered_email` | history only, through `effective_email` (`models.py:569-577`) |

`Notification.objects` is a `TenantAwareManager` (`models.py:544`). The feed
uses it; the history log deliberately does not, using `all_objects` plus an
explicit `tenant=` filter instead. That asymmetry is the source of the module's
worst defect (`notification_code_issues.md` §1).

## 3. Endpoint map

`?tenant=<slug>` is required on all eight routes: no view here sets
`tenant_param_required = False` (`vs_rbac/authentication.py:123-126`).

### Feed - `permission_classes = [IsAuthenticated]` (`views.py:118`)

| Method + path | query / body | response |
|---|---|---|
| `GET /` | `is_read`, `event_type_key`, `created_after`, `created_before`, `search` | Paginated `NotificationListSerializer`, unread first (`views.py:143-176`) |
| `GET /<uuid>/` | - | `NotificationDetailSerializer`, or `404` for anything not the caller's own in-app row (`views.py:178-197`) |
| `GET /unread-count/` | - | `{"unread_count": N}` (`views.py:199-210`) |
| `POST /mark-read/` | `{"ids": [uuid, …]}`, 1-100 | `{"updated_count": N}` (`views.py:212-242`) |
| `POST /mark-all-read/` | - | `{"updated_count": N}` (`views.py:244-263`) |
| `POST /acknowledge-route/` | `{"path": "/support/tickets/42"}` | `{"updated_count": N}` (`views.py:265-282`) |

### History - `[IsAuthenticated, HasRBACPermission]`, key `communication.message_activity.audit` (`views.py:304-305`)

| Method + path | query | response |
|---|---|---|
| `GET /history/` | `scope=platform`, `recipient_email`, `event_type_key`, `channel`, `status`, `created_after`, `created_before`, `search` | Paginated `NotificationHistorySerializer` (`views.py:373-393`) |
| `GET /history/<uuid>/` | - | `NotificationHistoryDetailSerializer` (adds `body`), or `404` (`views.py:395-406`) |

**At least one filter is mandatory on `/history/`.** With none supplied the view
returns `422` with code `FILTER_REQUIRED` (`views.py:342-346`,
`exceptions.py:85-96`), so nobody dumps a whole tenant's table in one call. A
search term counts as a filter.

Serializer field sets:

| Serializer | Fields |
|---|---|
| `NotificationListSerializer` (`serializers.py:50`) | `id`, `event_type_key`, `event_type_label`, `channel`, `subject`, `body`, `action_url`, `is_read`, `created_at` |
| `NotificationDetailSerializer` (`serializers.py:79`) | the above minus `body`-only framing, plus `status`, `read_at`, `dispatched_at` |
| `NotificationHistorySerializer` (`serializers.py:157`) | `id`, event key/label, `channel`, `subject`, `status`, `retry_count`, `failure_reason`, `recipient_name`, `recipient_email`, `tenant`, `dispatched_at`, `created_at` |
| `NotificationHistoryDetailSerializer` (`serializers.py:199`) | the above plus `body` |

## 4. Lifecycle / state machine

The only transition either surface drives is unread → read, and it is one-way:

```text
row created by dispatch  ──►  is_read = False, read_at = NULL
   POST /mark-read/          ──►  is_read = True,  read_at = now   (own IN_APP, unread only)
   POST /mark-all-read/      ──►  same, for every unread row
   POST /acknowledge-route/  ──►  same, for the rows whose event points at that path
```

There is no unread endpoint, no delete, and no archive. `mark_read` and
`mark_all_read` both filter `is_read=False` before updating
(`views.py:232-237,254-258`), so `read_at` records the first time a row was
read and is never overwritten.

## 5. Derivations

- **Feed order is `(is_read, -created_at, id)`** (`views.py:140`). Unread
  first, newest within each group, and the trailing `id` is not decoration:
  dispatch bulk-creates a batch of rows sharing one `created_at`, and without a
  unique tiebreaker the database may order those ties differently per query,
  which makes rows repeat or vanish between page 1 and page 2. Tested at
  `tests.py:926-952`.
- **`subject` falls back to the event label.** `get_subject` returns
  `obj.subject.strip() or obj.event_type.label` (`serializers.py:43-44`), which
  is what makes in-app rows readable at all - dispatch only renders a subject
  for templates that define one.
- **`action_url`** is resolved per row by `notification_action_url`
  (`services/routing.py:23-42`) from an allowlist, never from stored data
  directly: ticket and workflow events build a path from
  `metadata["ticket_id"]` / `["workflow_instance_id"]`, export failures from
  `["export_run_id"]`, and everything else falls back to a fixed prefix map.
  An unmatched event returns `""`.
- **`acknowledge-route` is the inverse map.** `notification_route_q`
  (`services/routing.py:52-74`) turns an allowlisted path back into an ORM `Q`.
  The metadata comparison tries both the string and the integer form of the id
  (`services/routing.py:45-49`), because callers store `ticket.pk` as an int and
  the URL carries it as text.
- **Path validation** rejects anything that is not a local route: it must start
  with a single `/`, must not contain `\`, `?` or `#`, and trailing slashes are
  normalised away (`serializers.py:145-150`). An external URL is a `400`
  (`tests.py:635-641`).
- **Search is a queryset filter, deliberately** (`views.py:163-168`). Filtering
  the page in the browser instead would leave the page count, the totals and
  every page after the first describing the unsearched list. Terms are truncated
  at 120 characters (`views.py:80`), because a 100k-character `icontains` scan
  is a free way to make the database work for nothing.
- **Feed search covers what the reader can see**: `subject`, `body`, event label
  (`views.py:84-90`). History search adds `recipient__email` and
  `unregistered_email` (`views.py:353-359`).
- **`recipient_name`** is the user's full name, falling back to their email,
  falling back to `unregistered_email` for an invitee with no account
  (`serializers.py:186-189`); `recipient_email` is `effective_email`
  (`models.py:569-577`).
- **`?scope=platform`** filters `tenant__kind="PLATFORM"` *on top of* the
  existing `tenant = request.tenant` scope (`views.py:348-349`), so it only ever
  returns rows for a caller who is themselves on the platform tenant. For a
  school admin it is a filter that always returns nothing.

## 6. What reading writes

The feed's three POST routes each run a single bulk `.update()` inside
`transaction.atomic()` (`views.py:230-237,252-258,273-278`). No signal fires, no
audit event is written, and nothing marks an email row read - email has no read
state, which is why `MarkReadSerializer` rejects email ids up front
(`serializers.py:125-139`).

The history log writes nothing at all. Reading another person's rendered
message body, their email address and a failure reason leaves no trace, in this
module or in `vs_audit`.

## 7. Worked example

```text
GET /v1/notify/?tenant=alpha-nt&search=invoice&is_read=false
```

```json
{ "success": true, "message": "Data retrieved successfully",
  "pagination": { "currentPage": 1, "pageSize": 25, "totalItems": 3,
                  "totalPages": 1, "next": null, "previous": null },
  "data": [
    { "id": "8f21…", "event_type_key": "billing.invoice_overdue",
      "event_type_label": "Invoice overdue", "channel": "in_app",
      "subject": "Invoice INV-2026-0417 is overdue",
      "body": "Dear Mr. Adeola Bakare,\n\nInvoice INV-2026-0417 …",
      "action_url": "", "is_read": false,
      "created_at": "2026-08-15T06:02:11Z" }
  ] }
```

`"action_url": ""` on a billing event is not a fixture artefact: the route table
maps `finance.` while the registry keys these events `billing.`
(`notification_code_issues.md` §4).

```text
GET /v1/notify/history/?tenant=alpha-nt&status=FAILED&channel=email
```

returns the same envelope with `status`, `retry_count`, `failure_reason`,
`recipient_email` and `tenant` per row. Calling it with no filter at all returns
`422` and `{"code": "FILTER_REQUIRED"}`.

## 8. Gotchas / known limitations

Full evidence for each is in
**`docs/notifications/notification_code_issues.md`**. The items belonging to
this slice:

- **The feed silently applies a second, undocumented tenant filter**, so rows
  dispatched under the initiating tenant never reach a recipient in a different
  one. This is the module's worst defect (`notification_code_issues.md` §1).
- **`acknowledge-route` uses `all_objects` while every sibling uses `objects`**
  (`views.py:273` vs `205,232,254`), so it can mark read a row the same user
  cannot see (`notification_code_issues.md` §3).
- **A school admin's history log can show CX staff email addresses**, because
  cross-tenant rows land under the school's tenant
  (`notification_code_issues.md` §1).
- **Eight active in-app event types have no destination** and three of the
  route table's prefixes match no event at all
  (`notification_code_issues.md` §4).
- **`mark-read` leaks a fact about ids it does not own**
  (`serializers.py:128-131`, `notification_code_issues.md` §6).
- **The mandatory-filter guard is decorative.** `?created_after=1970-01-01`
  satisfies it (`views.py:342-346`).
- **`created_after` / `created_before` are raw strings** on both surfaces
  (`views.py:155-161,366-369`), so a malformed value is a `500`, not a `400`
  (`notification_code_issues.md` §7).
- **`?is_read=` is a truthiness coin-flip**: anything that is not the literal
  `"true"` means `False` (`views.py:149`), so `?is_read=1` silently returns the
  read tab.
- **`_apply_filters` takes an `is_vision_staff` argument it never uses**
  (`views.py:327,379`) - dead scaffolding from when CX staff were unscoped.
- **Justified by design:** the feed returns `404`, never `403`, for another
  user's row (`views.py:178-194`). Existence is not leaked, and staff use the
  history endpoint. Tested at `tests.py:589-601`.
- **Justified by design:** search runs on the queryset rather than the page
  (`views.py:163-168`), and history search cannot reach outside the caller's
  tenant (`tests.py:852-864`).

## 9. Permissions & tenant isolation

| Surface | Gate | Seeded to |
|---|---|---|
| Feed (all six routes) | `IsAuthenticated` only | n/a - ownership is the boundary |
| History list + detail | `communication.message_activity.audit`, `SENSITIVE`, restricted | `xvs_super_admin`, `xvs_platform_admin`, and the `school_admin` / `branch_admin` prebuilts (`management/commands/seed_notification_permissions.py:23-35`) |

**The feed's isolation is ownership, and it holds.** Every queryset pins
`recipient = request.user`; the detail route reuses the same queryset rather
than fetching by pk (`views.py:186-189`), which is what makes the `404`
correct rather than incidental.

**The history log's isolation is `tenant = request.tenant`, and it also holds** -
for the rows that carry the right tenant. What it cannot fix is a row filed
under the wrong one: the log faithfully shows a school admin every row stamped
with their tenant, including notifications addressed to CX staff
(`notification_code_issues.md` §1).

`branch_admin` holds the same history key as `school_admin` with no branch
narrowing, because `Notification` has no branch column at all.

## 10. Code map

| File | Responsibility |
|---|---|
| `views.py:97-282` | `NotificationViewSet` - feed, counts, the three read-state writes |
| `views.py:289-406` | `NotificationHistoryViewSet` - scope, the eight filters, the mandatory-filter guard |
| `views.py:84-90` | `_feed_search_q` - the shared search predicate |
| `serializers.py:39-105` | Feed list/detail plus the `subject`/`action_url` presentation mixin |
| `serializers.py:112-150` | `MarkReadSerializer`, `AcknowledgeRouteSerializer` |
| `serializers.py:157-205` | History list and detail |
| `services/routing.py` | `notification_action_url`, `notification_route_q`, `_PREFIX_ROUTES` |
| `urls.py:59-110` | Route table (header comment names the wrong prefix - see the issues file) |
| `core/pagination.py` | `XVSPagination` - the `{pagination, data}` envelope both lists return |

## 11. Test coverage & gaps

- `FeedRetrieveTests` (`tests.py:587-694`) - another user's row is `404`;
  acknowledge-route marks only the matching ticket for the caller; an external
  URL is rejected; and a sweep asserting `notification_action_url` and
  `notification_route_q` stay aligned across modules.
- `FeedOrderAndSearchTests` (`tests.py:866-952`) - unread first then newest, the
  read filter, search narrowing the queryset so totals match, search matching
  the event label, search not reaching another user's inbox, search combined
  with the unread filter, and pagination stability under `created_at` ties.
- `HistoryScopingTests` (`tests.py:802-864`) - a school admin sees only their own
  tenant; the platform scope filter; the mandatory-filter `422`; search counting
  as a filter; search staying inside the caller's tenant.
- `ResponseShapeTests` (`tests.py:1334-1347`) - the `unread_count` object shape.

The suite is unusually good on the boundaries it knows about. What it does not
know about:

1. **The tenant filter on the feed.** Every fixture user reads their own
   tenant's rows, so nothing fails today for the cross-tenant case, and nothing
   would fail if the filter were removed. A test that dispatches with
   `tenant=<school>` to a CX recipient and then asserts the CX feed is the one
   that turns §8's first item red.
2. **`acknowledge-route` versus the tenant filter** - no test contrasts it with
   `mark-read` on the same row.
3. **`mark-all-read`** has no test at all, nor does `GET /<uuid>/` for the
   caller's own row (only the `404` case is covered).
4. **Malformed dates** on either surface, and `?is_read=1`.
5. **`?scope=platform` from a school caller** - nothing asserts it returns
   nothing rather than something.
6. **The empty-list response shape** on both lists, which matters because
   `success_response` coerces `[]` to `{}` (`core/response.py:6-11`).
7. **`recipient_name` / `recipient_email` for an unregistered recipient** in the
   history serializer.
