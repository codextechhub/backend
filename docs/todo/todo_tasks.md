# todo_tasks

The task itself: what a `Task` records, how one is created (self-set or handed
down), how it is edited, completed and deleted, and what the CRUD surface
returns. The tree that decides who may do any of it is `todo_hierarchy_scoping`;
the roll-up screens are `todo_dashboards_rollup`; the review request that fires
on completion is `todo_review_requests`.

Routes are mounted at `/v1/todo/` (`apps/urls.py:37`), from `vs_todo/urls.py`: a
DRF router on `tasks/` plus four function-style views.

---

## 1. What it is (and what it is NOT)

- **It is CX's internal accountability board, not a school-facing feature.**
  `vs_todo` is the platform intranet tool. The gate is
  `IsAuthenticatedAndActive & IsVisionStaff` (`views.py:33`), and `IsVisionStaff`
  means one thing: your tenant's kind is `PLATFORM`
  (`vs_rbac/permissions.py:240-244`).
- **A task has no tenant column**, and `Task.objects` is a plain manager
  (`models.py:26-83`). That is correct rather than an oversight: every row
  belongs to a CX staff member, and the whole table lives on the platform side.
  It also means none of the `TenantAwareManager` reasoning that applies
  everywhere else in the repo applies here.
- **Status is derived, never stored.** `Task.status` (`models.py:96-104`) is
  computed from `is_done` and `deadline` on every read. There is no status
  column, no transition table, and no state machine to violate.
- **One owner, always.** `assignee` is a single non-null FK. A task cannot be
  shared, reassigned through the API, or owned by a team.
- **Assignment only ever flows down.** A manager may hand a task to anybody
  beneath them in the CX organogram and to nobody else - not sideways, not
  upward, not to themselves (`services/hierarchy.py:126-131`).
- **The hierarchy is not on the row.** It is read live from
  `vs_user.Position.reports_to` every time a question is asked, so the ToDo tree
  always reflects the current org chart. See `todo_hierarchy_scoping`.
- **There is no audit trail.** Nothing in this module writes to `vs_audit` and
  there is no local log. A task's history is the row itself.
- **`DELETE` is a real delete** (`core/mixins.py:93-96`), and a manager may
  delete a report's task (§9).

## 2. Domain model

One model, `Task` (`models.py:26`), in table `vs_todo_task`.

| Field | Notes |
|---|---|
| `assignee` | `CASCADE`. The person accountable. Deleting the account deletes the tasks (§8) |
| `assigned_by` | `SET_NULL`, nullable. NULL means self-set. Deliberately survives the manager's account removal |
| `assigned_by_name` | Denormalized name snapshot, so the "assigned by" label survives that removal |
| `title` (200), `description` (text) | |
| `metric` (120), `target` (120) | The success measure and its goal, e.g. `Revenue` / `₦120M` |
| `deadline` | `DateField`, required. No validation against today (§8) |
| `priority` | `HIGH` / `MEDIUM` / `LOW` (`constants.py:8`) |
| `department` | Snapshot of the assignee's department at creation, so a historical task keeps it after they move team |
| `is_done` | Boolean |
| `completed_at` | Stamped by `mark_done`, cleared by `reopen` |

Four indexes (`models.py:78-83`): `(assignee, is_done)`, `(assignee, deadline)`,
`(assigned_by)`, `(is_done, deadline)`. They cover every query the module makes.

`Meta.ordering = ["is_done", "deadline", "-priority"]` - open before done,
soonest deadline first, and then a third term that does not do what it looks
like (§8).

`clean()` (`models.py:86-93`) holds exactly one invariant: `assigned_by` may not
point at the assignee, because a self-set task is modelled by a NULL. It is
enforced on the create path (`services/tasks.py:248`) and nowhere else - which
is safe here only because `assigned_by` is not writable through the update
serializer.

Three derived properties, all read-only and all serialized:

```python
status      -> COMPLETED if is_done; OVERDUE if deadline < today; else IN_PROGRESS
is_overdue  -> status == OVERDUE
is_self_set -> assigned_by_id is None
```

and two in-memory transitions, `mark_done()` and `reopen()`
(`models.py:115-125`), both idempotent and neither of which saves. Persisting is
`set_done`'s job (§6).

## 3. Endpoint map

Every route requires `?tenant=<slug>`: no view sets
`tenant_param_required = False` (`vs_rbac/authentication.py:128-131`). In
practice that is always `?tenant=codex`, because only a platform account gets
past `IsVisionStaff` at all.

No route declares an `rbac_permission`. The three seeded keys
(`todo.task.view`, `.manage`, `.assign`) are checked nowhere - see
`todo_hierarchy_scoping` §2.

| Method + path | Query / body | Response |
|---|---|---|
| `GET /tasks/` | `assignee`, `status` | Paginated `TaskSerializer` |
| `POST /tasks/` | `TaskWriteSerializer` | `201` + `TaskSerializer`, "Task created successfully." |
| `GET /tasks/<pk>/` | - | `TaskSerializer`, or `404` outside the viewer's area |
| `PUT` / `PATCH /tasks/<pk>/` | `TaskSerializer` fields | `TaskSerializer`, "Updated successfully." |
| `DELETE /tasks/<pk>/` | - | `200`, "Deleted successfully." |
| `POST /tasks/<pk>/toggle/` | `{"done": true\|false}` | `TaskSerializer`, "Task updated successfully." |

The four dashboard and picker routes (`dashboard/mine/`, `dashboard/team/`,
`dashboard/org/`, `assignable/`) are in `todo_dashboards_rollup` §3.

### List filters (`views.py:64-81`)

| Param | Effect |
|---|---|
| *(none)* | the viewer's own tasks only (`own_tasks_qs`) |
| `assignee=<id>` | that person's tasks, if they are inside the viewer's area; `403` otherwise |
| `status=COMPLETED\|IN_PROGRESS\|OVERDUE` | filtered in Python, because status is derived |

**The list is per-person, never per-area.** A manager cannot ask for "every open
task under me" in one call; they ask for one report at a time with
`?assignee=`, or read the roll-up dashboards. That is a deliberate consequence of
the design's screens, not an oversight.

**The status filter turns the queryset into a list** (`views.py:80`). It runs
before pagination, so the totals are right, but it materialises every one of the
person's tasks to do it. An unrecognised value is silently ignored (§8).

### Serializer field sets

| Serializer | Fields |
|---|---|
| `TaskSerializer` (`serializers.py:36`) | `id`, `title`, `description`, `metric`, `target`, `deadline`, `priority`, `department`, `assignee`, `assigned_by`, `assigned_by_name`, `is_done`, `completed_at`, `status`, `is_self_set`, `created_at`, `updated_at` |
| `TaskWriteSerializer` (`serializers.py:54`) | `title`, `description`, `metric`, `target`, `deadline`, `priority`, `assignee_id` |
| `PersonSerializer` (`serializers.py:19`) | `id`, `name`, `role`, `initials` |
| `ToggleSerializer` (`serializers.py:77`) | `done` |

`assignee` and `assigned_by` are expanded as `PersonSerializer` and are
read-only; `status` and `is_self_set` are read-only. **Everything else on
`TaskSerializer` is writable through `PATCH`**, including `is_done`,
`completed_at`, `department` and `assigned_by_name` - which is the module's worst
defect and is covered in §8.

`initials` (`serializers.py:29-31`) takes the first letter of the first two words
of the full name, falling back to the email when there is no name.

## 4. Lifecycle

There is no state machine, only a boolean and a date:

```text
                       toggle {"done": true}
   IN_PROGRESS  ───────────────────────────────►  COMPLETED
        │                                             │
        │  deadline passes                            │  toggle {"done": false}
        ▼                                             │
     OVERDUE  ◄───────────────────────────────────────┘
                       (if the deadline has passed by then)
```

Everything above is derived at read time. `OVERDUE` is not a transition anything
performs; it is what `status` returns once `deadline < today` and the task is not
done. A task therefore becomes overdue with no write, no signal and no
notification - nothing tells the assignee or their manager that it happened.

`mark_done` stamps `completed_at`; `reopen` clears it (`models.py:115-125`). Both
are no-ops when the task is already in the target state, so re-completing a
completed task does **not** re-stamp - which matters because the review request
keys on that stamp (`todo_review_requests` §5).

## 5. Derivations

- **Self-set versus assigned** is decided once, in `create_task`
  (`services/tasks.py:53`): `assignee is not None and assignee.pk != actor.pk`.
  Passing your own id as `assignee_id` is treated as self-set, not as an
  assignment to yourself, so it cannot trip the `clean()` invariant.
- **`assigned_by_name`** is `actor.full_name` at creation, and only for a real
  assignment (`services/tasks.py:65`). It is never recomputed, which is the
  point: the label survives the manager leaving.
- **`department`** is snapshotted from the *assignee*, not the actor
  (`services/tasks.py:72`), through `_department_for`
  (`services/tasks.py:26-32`): the DEPARTMENT-tier org node reached from their
  platform staff profile, or `""` when they have no profile or sit directly on a
  division.
- **`priority` defaults to `MEDIUM`** in the model, the enum and the write
  serializer (`models.py:62-64`, `serializers.py:66`).
- **The area check on `?assignee=`** is `int(assignee_id) not in
  TodoHierarchy.area_user_ids(viewer)` (`views.py:68`). `area_user_ids` includes
  the viewer, so `?assignee=<my own id>` is allowed and equals the default.
- **Object visibility** is the same question for one row:
  `can_view_task` (`services/tasks.py:121-123`) asks whether the task's assignee
  is in the viewer's area. `get_object` raises `NotFound`, not `PermissionDenied`
  (`views.py:88-89`), so a task outside your area is indistinguishable from one
  that does not exist.
- **Modification rights** are wider than ownership: `can_modify_task`
  (`services/tasks.py:126-130`) admits the assignee **or** anybody who could
  assign to them, which is everybody above them in the chain. One rule covers
  edit, delete and toggle.

## 6. What writing writes

Only the `Task` row. There is no audit event, no signal, and no history table.

| Operation | Path | Side effects |
|---|---|---|
| Create | `views.create` → `create_task` (`services/tasks.py:35`) | `full_clean()` then `save()`, inside `transaction.atomic`. No notification (§8) |
| Update | `views.update` → mixin → `serializer.save()` | Whatever the payload contained. No validation beyond field types |
| Delete | `views.destroy` → mixin → `instance.delete()` | The row is gone. Nothing records that it existed |
| Toggle | `views.toggle` → `set_done` (`services/tasks.py:79`) | `is_done`, `completed_at`, `updated_at`; and, on a genuine self-completion, a queued review request |

`set_done` is the only write that reaches outside the row, and only in one
direction: when the **assignee** completes their **own** task
(`services/tasks.py:96-98`), a review request is queued on
`transaction.on_commit` with a short countdown. A manager ticking a report's task
never triggers it. Full detail in `todo_review_requests`.

`create_task` is the only write that calls `full_clean()`. The update path does
not, which is currently harmless because the one invariant concerns a field the
update serializer does not expose.

## 7. Worked example

Chidi (Head of Sales) hands a task to Tobi (Sales Rep, who reports to him):

```text
POST /v1/todo/tasks/?tenant=codex
{"title": "Close the Q3 renewal list", "description": "All 14 accounts.",
 "metric": "Renewals closed", "target": "14", "deadline": "2026-09-30",
 "priority": "HIGH", "assignee_id": 41}
```

```json
{ "success": true, "message": "Task created successfully.",
  "data": {
    "id": 208, "title": "Close the Q3 renewal list",
    "metric": "Renewals closed", "target": "14",
    "deadline": "2026-09-30", "priority": "HIGH", "department": "Sales",
    "assignee":    {"id": 41, "name": "Tobi Member", "role": "Sales Rep",  "initials": "TM"},
    "assigned_by": {"id": 22, "name": "Chidi Head",  "role": "Head of Sales", "initials": "CH"},
    "assigned_by_name": "Chidi Head",
    "is_done": false, "completed_at": null,
    "status": "IN_PROGRESS", "is_self_set": false,
    "created_at": "2026-08-21T10:04:11Z", "updated_at": "2026-08-21T10:04:11Z" } }
```

Tobi is not told (§8). He finds it the next time he opens the tool.

If Chidi had aimed one level up instead:

```text
POST /v1/todo/tasks/?tenant=codex   {"title": "…", "deadline": "…", "assignee_id": 7}
  → 403  "You can only assign tasks to people within your team."
```

Tobi finishes it and ticks it off:

```text
POST /v1/todo/tasks/208/toggle/?tenant=codex   {"done": true}
  → is_done true, completed_at stamped, a review request queued for Chidi
```

But this also works, and does something different:

```text
PATCH /v1/todo/tasks/208/?tenant=codex   {"is_done": true}
  → is_done true, completed_at STILL NULL, no review request, status COMPLETED
```

That second call is §8's first item.

## 8. Gotchas / known limitations

Full evidence for each is in **`error/todo/todo_code_issues.md`**. The items
belonging to this slice:

- **`PATCH` can complete a task without stamping it and without telling the
  manager.** `is_done`, `completed_at`, `department` and `assigned_by_name` are
  all writable on `TaskSerializer` (`serializers.py:41-48`), despite the comment
  at `views.py:124-125` saying only descriptive fields are editable
  (`todo_code_issues.md` §1).
- **Nothing is audited, and a manager can hard-delete a report's task**
  (`views.py:128-132`, `core/mixins.py:93-96`). In a tool whose purpose is
  accountability, the record can be removed without trace
  (`todo_code_issues.md` §2).
- **Being handed a task notifies nobody.** `EVENT_TASK_ASSIGNED`
  (`constants.py:41`) is declared, is not in the notification registry, and is
  fired from nowhere (`todo_code_issues.md` §3).
- **High-priority tasks sort last.** `-priority` (`models.py:77`) is a
  descending sort on the stored strings, so the order within a deadline group is
  `MEDIUM`, `LOW`, `HIGH` (`todo_code_issues.md` §5).
- **Deleting a CX account deletes their tasks.** `assignee` is `CASCADE`
  (`models.py:37-41`) while `assigned_by` is `SET_NULL` with a name snapshot
  specifically so the record survives - the two halves of the same row disagree
  about whether history matters (`todo_code_issues.md` §6).
- **`?assignee=abc` is a 500.** `int(assignee_id)` (`views.py:68`) raises
  `ValueError`, which is not a handled exception type
  (`todo_code_issues.md` §7).
- **An unknown `?status=` is ignored, not refused** (`views.py:78`), so
  `?status=DONE` quietly returns everything (`todo_code_issues.md` §8).
- **A deadline in the past is accepted**, and the task is `OVERDUE` from the
  moment it is created (`todo_code_issues.md` §14).
- **`TaskWriteSerializer` tells a caller whether a user id exists**
  (`serializers.py:69-74`): "No such user." for a free id, and a `403` about
  teams for a real one (`todo_code_issues.md` §15).
- **`update` and `destroy` resolve the object twice** (`views.py:118,121` and
  `128,132`), and each resolution rebuilds the whole position tree
  (`todo_code_issues.md` §16 item 1).
- **Becoming overdue is silent.** No sweep, no notification, no flag - only a
  property that starts answering differently.
- **Justified by design:** a task outside your area is a `404`, never a `403`
  (`views.py:88-89`).
- **Justified by design:** `Task` carries no tenant and uses a plain manager -
  the whole table is platform-owned.

## 9. Permissions & tenant isolation

| Action | Gate |
|---|---|
| Every route in the module | `IsAuthenticatedAndActive & IsVisionStaff` - an active account on a `PLATFORM` tenant |
| See a task | `can_view_task` - the assignee is in your area |
| Create for yourself | nothing beyond the gate |
| Create for someone else | `TodoHierarchy.can_assign` - strictly beneath you |
| Edit, delete, toggle | `can_modify_task` - you are the assignee, or above them |

**There is no RBAC key anywhere in the request path.** The three keys the seeder
registers exist for future wiring and are read by nothing today
(`constants.py:35-38`), so the practical boundary is "CX staff" plus the
organogram. What that means in the small - a brand-new platform account with no
seat sees an empty tool and can create tasks for itself, and nothing else - is
`todo_hierarchy_scoping` §3.

Tenant isolation is not a question this module answers, because there is only one
tenant on this side of the wall. The `?tenant=` parameter is still required by
the auth layer, and asserting any slug other than `codex` fails there
(`vs_rbac/authentication.py:119-127`) before a view runs.

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py:26-128` | `Task` - fields, indexes, `clean()`, derived status, the two in-memory transitions |
| `constants.py` | `Priority`, `TaskStatus`, the three unused RBAC keys, the two event keys, `REVIEW_GRACE_SECONDS` |
| `views.py:51-145` | `TaskViewSet` - queryset, filters, create, update, destroy, toggle |
| `views.py:36-46` | `_resolve_focus` - shared with the team dashboard |
| `services/tasks.py:35-76` | `create_task` - the self-set/assignment fork, the department snapshot |
| `services/tasks.py:79-118` | `set_done` - persist plus the review-request hand-off |
| `services/tasks.py:121-130` | `can_view_task`, `can_modify_task` |
| `serializers.py:19-80` | `PersonSerializer`, `TaskSerializer`, `TaskWriteSerializer`, `ToggleSerializer` |
| `core/mixins.py` | The envelope-wrapping retrieve/create/update/destroy |
| `core/pagination.py` | `XVSPagination` - the `{pagination, data}` envelope the list returns |

## 11. Test coverage & gaps

- `TaskStatusTests` (`tests.py:117-145`) - the three-way status derivation and
  `stats_for`'s counts and percentage.
- `TaskServiceTests` (`tests.py:147-181`) - a self-set task leaves
  `assigned_by` NULL; an assignment downward records the manager and snapshots
  the department; an assignment upward raises `PermissionDenied`; `set_done`
  stamps `completed_at`.
- `HierarchyTests` and `DashboardTests` cover the tree and the roll-up; see the
  other slices.

What the suite does not cover:

1. **The HTTP surface, at all.** There is no `APIClient` anywhere in
   `tests.py`. Nothing exercises `IsVisionStaff`, the `?assignee=` area check,
   the `?status=` filter, the `404` for a task outside the area, the pagination
   envelope, or the empty-list response shape - which matters because
   `success_response` coerces `[]` to `{}` (`core/response.py:6-11`).
2. **`PATCH`.** No test writes to a task through the update serializer, which is
   why §8's first item passes a green suite.
3. **`DELETE`**, and whether a manager may delete a report's task.
4. **`can_view_task` / `can_modify_task`** directly - both are only exercised
   transitively through `can_assign`.
5. **Ordering.** Nothing asserts the order tasks come back in, which is why the
   `-priority` bug is invisible.
6. **`reopen`** and the toggle-off path.
7. **A past deadline**, an unknown `?status=`, and a non-numeric `?assignee=`.
8. **A user with no organogram seat** creating and reading their own tasks.
