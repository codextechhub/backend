# todo_code_issues

Everything wrong with `vs_todo`, in one place, ordered by how much it costs. Each
item states the defect, the evidence, what actually happens to a person, and the
fix. The four slice reports (`todo_tasks`, `todo_hierarchy_scoping`,
`todo_dashboards_rollup`, `todo_review_requests`) point here rather than
repeating it.

**Nothing here is graded Critical, and that is a real finding rather than
generosity.** `vs_todo` holds one tenant's data, has no cross-tenant surface, and
exposes nothing to a school account: `IsVisionStaff` keeps them out and the CX
organogram cannot contain them. What is at risk in this module is the integrity
of the accountability record itself, which is the only thing it is for.

Baseline: the `vs_todo` suite is **`Ran 21 tests in 15.310s` - FAILED
(errors=1)** (`cd apps && DB_NAME=cx_todo_doc ../cx/Scripts/python.exe manage.py
test vs_todo --settings=apps.settings.local --noinput`). The one error is
environmental rather than a logic failure and belongs to `core`, not to this
module: `test_seed_all_permissions_runs_clean` (`tests.py:333-337`) calls
`seed_all_permissions`, which prints a box-drawn banner
(`core/management/commands/seed_all_permissions.py:95-98`) that a Windows cp1252
stream cannot encode, so the command dies with `UnicodeEncodeError`. It is the
same class as the `vs_user` baseline already recorded in the playbook, and it
makes the suite red on Windows and green elsewhere. The other twenty tests pass.

Every item below is therefore something those twenty do not catch. Every claim is
traced to a file and line. Nothing here is speculative.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in the
code.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | A person can mark their own work done without their manager ever being told, and can rewrite who gave them the task | **High** |
| 2 | Nothing is audited, and a manager can permanently delete a report's task | **High** |
| 3 | Being handed a task notifies nobody | **High** |
| 4 | The undo window is five seconds, and zero everywhere it is currently deployed | **Medium** |
| 5 | High-priority tasks sort last | **Medium** |
| 6 | Deleting a CX account erases every task they were accountable for | **Medium** |
| 7 | Two query parameters answer 500 on a value the caller chooses | **Medium** |
| 8 | An unrecognised `?status=` is ignored rather than refused | **Medium** |
| 9 | The team dashboard rebuilds the whole org tree once per report | **Medium** |
| 10 | Three screens return every task ever recorded, unpaginated | **Medium** |
| 11 | Three seeded permission keys are checked nowhere, and the gate is "any platform account" | **Medium** |
| 12 | The org roll-up recurses with no cycle guard, while its two siblings have one | **Low** |
| 13 | A review request has nowhere to click | **Low** |
| 14 | A task can be created already overdue | **Low** |
| 15 | Task creation tells a caller whether a user id exists | **Low** |
| 16 | Smaller defects and dead code | **Low** |

---

## 1. A person can mark their own work done without their manager ever being told, and can rewrite who gave them the task

**High. The module's purpose, walked around with one field.**

### The defect

`TaskViewSet.update` runs the ownership check and then hands off to the generic
mixin with `TaskSerializer`:

```python
# views.py:117-126
def update(self, request, *args, **kwargs):
    task = self.get_object()
    if not tasks_svc.can_modify_task(request.user, task):
        raise PermissionDenied("You cannot edit this task.")
    return super().update(request, *args, **kwargs)

def perform_update(self, serializer):
    # Only the descriptive fields are editable here; ownership/assignment is
    # set at creation and not reshuffled through a plain PATCH.
    serializer.save()
```

That comment is false. `TaskSerializer` (`serializers.py:36-52`) lists every
field and sets `read_only` on four of them only - `assignee`, `assigned_by`
(both nested `PersonSerializer`s), `status` and `is_self_set`. There is no
`read_only_fields`. `id`, `created_at` and `updated_at` are read-only because DRF
infers it. Everything else is writable:

```
title  description  metric  target  deadline  priority
department  assigned_by_name  is_done  completed_at
```

The last four are not descriptive fields. They are the record.

### What actually happens

Tobi has a task from his head of sales, Chidi: *"Close the Q3 renewal list,
target 14."* The proper way to finish it is
`POST /v1/todo/tasks/208/toggle/ {"done": true}`, which stamps `completed_at` and
queues a review request so Chidi is emailed and gets a bell notification
(`todo_review_requests`).

Instead Tobi sends:

```text
PATCH /v1/todo/tasks/208/?tenant=codex   {"is_done": true}
```

`200 OK`. The task now reads `status: "COMPLETED"`, it disappears from every
overdue count and rises to the top of Tobi's completion percentage on Chidi's
dashboard - and:

- `completed_at` is still `null`, so nobody can say when it was done;
- `set_done` never ran, so **no review request was queued and Chidi is never
  told**;
- nothing anywhere records that this happened (§2).

The same call reaches further. `PATCH {"assigned_by_name": "Ada Director"}`
rewrites the label the UI shows for who handed the task down - the snapshot the
model comments describe as existing so the record survives a manager leaving.
`PATCH {"department": "Engineering"}` rewrites the departmental attribution the
roll-ups are grouped by. `PATCH {"completed_at": null}` on a done task, or a
`completed_at` in the future, produces a state no code path can create.

And a manager can do all of it to a report's task, because `can_modify_task`
admits anybody above the assignee (`services/tasks.py:126-130`).

### Why it exists

`TaskSerializer` was written as the **read** serializer - the docstring says
"Full read view of a task, with derived status and people expanded"
(`serializers.py:37`) - and then reused as `serializer_class` on a
`ModelViewSet`, where DRF makes it the write serializer too. The create path
avoids this by using a dedicated `TaskWriteSerializer`; the update path never got
one.

### The fix

Fix the class, not the case:

1. **Give the update path its own serializer**, the way create already has one -
   `title`, `description`, `metric`, `target`, `deadline`, `priority` and nothing
   else. That is what the comment at `views.py:124-125` already claims.
   Failing that, put `read_only_fields` on `TaskSerializer` naming
   `is_done`, `completed_at`, `department` and `assigned_by_name`.
2. **Make `set_done` the only way `is_done` changes.** Completion has side
   effects - a stamp and a notification - and any path that sets the flag without
   them produces a record that is wrong in a way nobody can see.
3. Add the test: `PATCH {"is_done": true}` must be refused, and a completion
   through `toggle` must be the only thing that stamps `completed_at`.

---

## 2. Nothing is audited, and a manager can permanently delete a report's task

**High. In a tool whose entire subject is accountability.**

### The defect

`vs_todo` contains no call to `emit_audit_event`, no local log model, and no
signal. Grep the app for "audit" and the only hit is an unrelated permission key
in a test (`tests.py:303`).

Meanwhile `DELETE` is wired and real:

```python
# views.py:128-132
def destroy(self, request, *args, **kwargs):
    task = self.get_object()
    if not tasks_svc.can_modify_task(request.user, task):
        raise PermissionDenied("You cannot delete this task.")
    return super().destroy(request, *args, **kwargs)
```

`super().destroy` is `core/mixins.py:88-96`: `instance.delete()`. A hard delete,
no soft-delete flag, no archive.

`can_modify_task` (`services/tasks.py:126-130`) admits the assignee **or**
anybody who can assign to them - which is everyone above them in the chain, at
any depth.

### What actually happens

Chidi assigns Tobi a task with an uncomfortable target. Two months later the
target was missed, and the quarterly review is next week.

```text
DELETE /v1/todo/tasks/208/?tenant=codex
  → 200 {"success": true, "message": "Deleted successfully."}
```

The task is gone. Tobi's overdue count drops by one, his completion percentage
rises, Chidi's area percentage rises, and there is no record anywhere - not in
`vs_audit`, not in a log table, not on the row, because there is no row - that
the task ever existed or that Chidi removed it. The same is true of every `PATCH`
in §1: a deadline moved from September to December leaves no before-and-after
anywhere.

Compare `vs_tickets`, which refuses `DELETE` outright with *"Tickets are retained
for audit history and cannot be deleted"* and writes two audit records for every
single write. The reasoning that produced that rule applies here at least as
strongly.

### The fix

1. **Emit audit events for create, update, delete and completion.**
   `emit_audit_event` takes `entity_type`/`entity_id`/`before_data`/`diff_data`
   and `vs_tickets`' `record_ticket_audit` (`vs_tickets/services/audit.py:13`) is
   a working model to copy, including its impersonation handling.
2. **Reconsider hard delete.** Either remove `DestroyModelMixin` from the viewset
   and let tasks be closed rather than erased, or restrict deletion to the
   assignee's own self-set tasks. A manager deleting a report's task is the
   single action in this module most in need of a record.
3. Add the test: a manager deleting a report's task, and what is left behind.

---

## 3. Being handed a task notifies nobody

**High. The one moment the design is about is silent.**

### The defect

```python
# constants.py:41-42
EVENT_TASK_ASSIGNED  = "todo.task_assigned"
EVENT_TASK_COMPLETED = "todo.task_completed"
```

`EVENT_TASK_COMPLETED` is registered in `vs_notifications`
(`vs_notifications/constants.py:634-641`), templated
(`vs_notifications/services/seed.py:1207-1230`) and fired from `tasks.py:86`.

`EVENT_TASK_ASSIGNED` is none of those things. It is not in the event registry,
has no template, and appears in no code path anywhere in the repo except its own
declaration. `create_task` (`services/tasks.py:35-76`) creates the row and
returns; there is no notification hook of any kind on the create path.

### What actually happens

Ada, the MD, is preparing for a board meeting. She opens the ToDo tool and hands
Chidi a task: *"Board pack: Q3 pipeline summary, due Friday."*

Chidi is not emailed. He gets no bell notification. There is no digest and no
daily summary. He finds out on Thursday, when he happens to open Tasks → My
Tasks for another reason, and discovers a High-priority item due tomorrow that
has been sitting there for six days.

The module already has everything needed to prevent this: the notification engine
is wired, the reviewer-side flow proves the integration works, and the event key
is already chosen and written down.

### The fix

1. **Register `todo.task_assigned`** in `EVENT_TYPE_REGISTRY` with `IN_APP` and
   `EMAIL`, `source_module="vs_todo"`, and seed a template pair, following the
   `todo.task_completed` entry directly above it.
2. **Fire it from `create_task`**, on `transaction.on_commit`, only when
   `is_assignment` is true (the same condition that already decides
   `assigned_by`), to the assignee, with a context mirroring
   `send_completion_review_request`'s.
3. While there: consider a deadline reminder. A task going overdue is equally
   silent, and for the same reason.

---

## 4. The undo window is five seconds, and zero everywhere it is currently deployed

**Medium. A carefully built mechanism, defeated by one constant and one setting.**

### The defect

```python
# constants.py:44-46
# Grace window between a user self-completing a task and the review request
# going out. The Celery task is queued with this countdown and re-checks the
# task's state at send time, so undoing within the window cancels the email.
REVIEW_GRACE_SECONDS = 5
```

The mechanism around it is good: `set_done` queues on `transaction.on_commit`
with `countdown=REVIEW_GRACE_SECONDS` (`services/tasks.py:99-118`), and the task
re-reads the row at send time with three guards rather than trying to cancel a
queued job (`tasks.py:54-65`). That design is right.

Five seconds is not an undo window for a human being. A person who mis-clicks
"done" on the wrong row notices when the row moves, reads it, and reaches for the
toggle - well past five seconds.

**And in every environment this repo currently configures, the countdown does not
apply at all:**

| Setting | `CELERY_TASK_ALWAYS_EAGER` |
|---|---|
| `apps/settings/local.py:33` | `True` |
| `apps/settings/ci.py:28` | `True` |
| `apps/settings/test.py:43` | `True` |
| `apps/settings/staging.py:45` | `config("CELERY_EAGER", default=True)` |

Under eager mode Celery runs `apply_async` inline and **ignores `countdown`
entirely**. The review request is dispatched the instant the toggle's transaction
commits.

### What actually happens

On staging, Tobi ticks the wrong row, sees his mistake immediately, and un-ticks
it. Chidi has already been emailed *"Tobi Member has marked 'Close the Q3 renewal
list' as done"*, and there is no follow-up saying otherwise - the flow has no
retraction path, only a cancellation that can no longer happen.

There is a second consequence on staging alone.
`CELERY_TASK_EAGER_PROPAGATES = CELERY_TASK_ALWAYS_EAGER`
(`apps/settings/staging.py:46`), and `send_completion_review_request` catches
exactly one exception type, `UnknownEventTypeError` (`tasks.py:89`). Anything
else the notification engine raises - a template that fails to render, a missing
row - propagates out of the `on_commit` callback and out of `set_done`'s atomic
block, so **the toggle answers 500 on a completion that has already been
committed.** The user sees an error, refreshes, and finds the task marked done.

### The fix

1. **Raise the window to something a person can use** - 60 to 120 seconds. The
   re-check design costs nothing extra for a longer wait.
2. **Do not rely on `countdown` under eager mode.** Either exclude eager
   environments from the flow, or gate the dispatch on a real elapsed-time check
   against `completed_at` inside the task, which works in both modes.
3. **Widen the exception handling** in `send_completion_review_request` to catch
   and log anything, returning a skip. A notification failure must never fail the
   toggle, and today it does exactly that on staging.

---

## 5. High-priority tasks sort last

**Medium. One character, on the field it matters most for.**

```python
# models.py:77
ordering = ["is_done", "deadline", "-priority"]
```

`priority` is a `CharField` holding `"HIGH"`, `"MEDIUM"` or `"LOW"`
(`constants.py:8-11`). `-priority` sorts those strings descending:

```
MEDIUM  >  LOW  >  HIGH
```

So within a deadline group the order is Medium, Low, **High** - the urgent work
last. Ascending would give `HIGH, LOW, MEDIUM`, which is also wrong; there is no
alphabetical order of those three words that is the intended order.

**What actually happens.** Tobi opens My Tasks on the morning of the 30th, with
four things due that day: two Medium, one Low, one High. The High one - the
renewal list Chidi is waiting on - is at the bottom of the four, under a
low-priority housekeeping item. On a screen with pagination, on a busy week, it
is on the second page.

**The fix.** Order on a rank, not on the stored text:

```python
from django.db.models import Case, IntegerField, Value, When
PRIORITY_RANK = Case(
    When(priority=Priority.HIGH, then=Value(0)),
    When(priority=Priority.MEDIUM, then=Value(1)),
    default=Value(2), output_field=IntegerField(),
)
```

annotated in the querysets that need it, or store an integer rank alongside the
label. Then assert the order in a test - nothing does today, which is why this
survived (§ "What the test suite does not know").

---

## 6. Deleting a CX account erases every task they were accountable for

**Medium. The two halves of the same row disagree about whether history matters.**

```python
# models.py:37-52
assignee = models.ForeignKey(..., on_delete=models.CASCADE, related_name="todo_tasks")

# The manager who handed this task down. NULL == the assignee set it for
# themselves. SET_NULL so the task survives the manager's account removal.
assigned_by = models.ForeignKey(..., on_delete=models.SET_NULL, null=True, blank=True, ...)
# Denormalized snapshot of the inviter's name, so an assigned-by label
# survives even after the manager's account is removed.
assigned_by_name = models.CharField(max_length=200, blank=True, default="")
```

The module goes to the trouble of a `SET_NULL` **and** a denormalized name
snapshot so that a departed manager's name still appears on the task. Two lines
above, the assignee is `CASCADE`.

**What actually happens.** Tobi leaves CodeX. Someone deletes his account during
the offboarding tidy-up. Every task he was ever accountable for - forty rows,
including the ones Chidi and Ada assigned to him, the ones that were missed, and
the ones that were completed - disappears in the same statement. Chidi's area
percentage changes retroactively. The quarterly numbers no longer reconcile with
what anyone remembers, and nothing records why.

Note the mitigating fact: the platform deactivates rather than deletes accounts
in normal operation (`User.status`), and `_holders` filters on `is_active`
(`services/hierarchy.py:60-63`), so a deactivated person simply drops out of the
tree with their tasks intact. The cascade only bites on an actual row deletion -
a cleanup script, an admin action, a test fixture teardown reused in a data fix.
That is exactly the situation in which nobody is watching.

**The fix.** `PROTECT` the assignee, matching `vs_tickets`' requester
(`vs_tickets/models.py:81-85`), and add a name snapshot for symmetry with
`assigned_by_name` if the record needs to survive the account. A deletion that
must proceed can then reassign or archive first, deliberately.

---

## 7. Two query parameters answer 500 on a value the caller chooses

**Medium.**

```python
# views.py:44 - _resolve_focus
if int(focus_id) not in TodoHierarchy.area_user_ids(viewer):

# views.py:68 - TaskViewSet.get_queryset
if int(assignee_id) not in TodoHierarchy.area_user_ids(viewer):
```

Both call `int()` on a raw query parameter. `?focus=abc` or `?assignee=null`
raises `ValueError`, which `custom_exception_handler` does not intercept
(`core/exceptions.py:91-195`) - it falls through to the final branch and answers
`500` with code `SERVER_ERROR`, plus a logged exception.

`?assignee=` and `?focus=` empty are safe: the truthiness check above each
`int()` short-circuits. But `null` is a value frontends genuinely send, and it is
a 500.

**The fix.** Coerce once, refuse cleanly:

```python
try:
    focus_pk = int(focus_id)
except (TypeError, ValueError):
    raise ValidationError({"focus": "Must be a user id."})
```

or validate the two parameters through a small query serializer. Same class of
defect as `vs_tickets` (`error/tickets/ticket_code_issues.md` §9); worth fixing
in both while the shape is fresh.

---

## 8. An unrecognised `?status=` is ignored rather than refused

**Medium.**

```python
# views.py:75-80
status_filter = self.request.query_params.get("status")
if status_filter:
    wanted = status_filter.upper()
    if wanted in TaskStatus.values:
        qs = [t for t in qs if t.status == wanted]
```

If `wanted` is not a known value, the branch is skipped and **the unfiltered list
is returned**, with a `200` and no indication that the filter was dropped.

**What actually happens.** The frontend's "Completed" tab is built against the
design's vocabulary and sends `?status=DONE` (or `?status=Complete`, or
`?status=completed` after someone removes the `.upper()`). The tab shows every
task the person has, open ones included, and looks like a data bug rather than a
parameter bug. Nobody gets an error to chase.

Two smaller notes on the same lines: the comment says "filter in Python on the
(small) page set", but the filter runs **before** pagination, over the whole
queryset - which is the correct order, since it keeps the totals honest, but it
does materialise every one of the person's tasks to do it. And `get_queryset`
returns a `list` rather than a `QuerySet` in this branch, which DRF paginates
happily but which quietly disables any downstream queryset operation.

**The fix.** Answer `400` on an unknown value, and note in the docstring that the
filter is deliberately in Python because `status` is derived.

---

## 9. The team dashboard rebuilds the whole org tree once per report

**Medium. And the fix is already written, in the same file.**

```python
# services/dashboards.py:28-33
for report in TodoHierarchy.direct_report_users(focus):
    reports.append({
        "person": report,
        "is_manager": TodoHierarchy.is_manager(report),
        "area_stats": stats_for(area_tasks_qs(report)),
    })
```

Every one of those three calls goes back to the database:

- `is_manager(report)` → `primary_position` (1 query) + `_children_index()`
  (1 query + a prefetch over all positions);
- `area_tasks_qs(report)` → `area_user_ids` → `descendant_users` →
  `primary_position` + `_children_index()` again;
- `stats_for(...)` → evaluates that queryset (1 more).

So roughly five queries and **two full passes over the `Position` table per
direct report**, plus the same again for the focus person. A head with eight
reports draws one screen with around forty queries and seventeen scans of the
position table.

Twenty lines further down, `org_rollup` does the whole tree properly
(`services/dashboards.py:57-63`): `_children_index()` **once**, one task fetch for
the entire area grouped by assignee, and area stats accumulated upward through
the recursion. Two queries for an arbitrarily deep tree.

**The fix.** Build `node_dashboard` on the same primitives: one
`_children_index()`, one `Task.objects.filter(assignee_id__in=area_user_ids(focus))`
grouped in Python, and derive each report's `is_manager` from the index already
in hand. Alternatively, memoise `_children_index` per request.

---

## 10. Three screens return every task ever recorded, unpaginated

**Medium.**

| Where | Code |
|---|---|
| `GET /dashboard/mine/` → `tasks` | `views.py:159` |
| `GET /dashboard/team/` → `own_tasks` | `services/dashboards.py:26` |
| `GET /dashboard/org/` | the whole subtree, `services/dashboards.py:46-92` |

`own_tasks_qs` is `Task.objects.filter(assignee=user)` (`services/stats.py:38-40`)
with no window and no limit. `TaskViewSet.list` is paginated by
`XVSPagination` (`apps/settings/base.py:66-67`); none of these three is.

A person two years into the tool has every task they have ever been given,
completed ones included, in every dashboard response, on every refresh. The
`stats` alongside them need the full set - that part is correct - but the task
*list* does not.

**The fix.** Compute the stats from an aggregate rather than by iterating rows,
and return a bounded task list (open tasks plus the most recently completed, or a
paginated sub-resource). `org_rollup` needs no task list at all, only counts.

---

## 11. Three seeded permission keys are checked nowhere, and the gate is "any platform account"

**Medium.**

```python
# constants.py:32-38
# Access to the ToDo tool is gated to CX staff; *what* a person may see and who
# they may assign to is then enforced structurally by the organogram ...
# These keys exist for future fine-grained wiring through the RBAC registry;
# today the views gate on CX-staff membership.
PERM_TASK_VIEW   = "todo.task.view"
PERM_TASK_MANAGE = "todo.task.manage"
PERM_TASK_ASSIGN = "todo.task.assign"
```

`manage.py seed_todo_permissions` registers all three and grants them to
`xvs_super_admin` and `xvs_platform_admin`
(`management/commands/seed_todo_permissions.py:15-25,95-129`), and
`test_todo_seed_captures_and_grants_task_keys` (`tests.py:323-331`) asserts that
it did. No view sets `rbac_permission`; no service calls
`user_has_rbac_permission`. The keys do nothing.

The real gate is:

```python
# views.py:33
TODO_PERMISSIONS = [IsAuthenticatedAndActive & IsVisionStaff]
```

and `IsVisionStaff` is one line: `user.tenant.kind == "PLATFORM"`
(`vs_rbac/permissions.py:240-244`).

**What actually happens.** A contractor is given a platform account so they can
be reached in the console. They have no seat in the organogram and no role
grants. They can reach every `/v1/todo/` route: their dashboards are empty, their
assignable list is empty, and they can create tasks for themselves. Harmless -
but the boundary that makes it harmless is the *absence of a seat*, not any
decision anybody made about them.

Now give that account a seat, for an unrelated reason - so they appear on the
org chart - and they immediately see the completion percentages of everybody
beneath that seat, with names and roles, and can assign work to them. There is no
key to withhold, because the keys are decorative.

**The fix.** Either wire the three keys (`todo.task.view` on the read surfaces,
`todo.task.assign` on `create` when `assignee_id` is present and on
`assignable/`, `todo.task.manage` on update/delete/toggle) or delete them from
the seeder and state plainly in the docstring that organogram membership is the
whole boundary. Leaving three granted keys that mean nothing invites the next
reader to trust them.

---

## 12. The org roll-up recurses with no cycle guard, while its two siblings have one

**Low, but a hard hang if it fires.**

```python
# services/dashboards.py:66-79
def build(position):
    ...
    for child_pos in index.get(position.pk, []):
        node = build(child_pos)
```

No `seen` set, no depth limit. Compare, in the same module:

```python
# services/hierarchy.py:79-87 - descendant_users
seen_positions: Set[int] = {position.pk}
while stack:
    seat = stack.pop()
    if seat.pk in seen_positions:
        continue

# services/hierarchy.py:165-167 - chain_to
seen_positions: Set[int] = {position.pk}
while seat is not None and seat.pk not in seen_positions:
```

Both upward and downward walks in `hierarchy.py` defend against a cycle. The
recursion in `dashboards.py` does not.

`Position.clean()` forbids cycles (`vs_user/models.py:1360-1369`), but `clean()`
runs only from forms and serializers - not from `Position.objects.update()`, not
from a data migration, not from a `bulk_update`, and not from the shell. If a
cycle ever exists, `descendant_users` returns a sensible answer and
`/v1/todo/dashboard/org/` recurses until Python raises `RecursionError`, which
the handler turns into a `500`.

**The fix.** Carry a `seen_positions` set through `build`, matching the two
functions next door. Three lines.

---

## 13. A review request has nowhere to click

**Low. One instance of a known `vs_notifications` gap.**

`send_completion_review_request` dispatches `todo.task_completed` with no
`metadata` (`tasks.py:83-88`), and `vs_notifications`'s router has no `todo.`
entry: `_PREFIX_ROUTES` covers imports, exports, team management, security,
finance and procurement, and the three special cases are tickets, workflow and
exports (`vs_notifications/services/routing.py:13-41`). Anything unmatched
returns `""`.

So every review request's in-app row has `action_url: ""`. The seeded template
compensates in prose - *"Kindly review it under Tasks → My Team"*
(`vs_notifications/services/seed.py:1208-1213`) - which is the tell: the body
tells the reader where to navigate because the notification cannot take them
there.

This is one of the eight in-app event types with no destination recorded in
`error/notifications/notification_code_issues.md` §4.

**The fix.** Pass `metadata={"task_id": task.pk}` from `tasks.py`, and add a
ticket-style case to `notification_action_url` resolving
`todo.` + `task_id` to the team dashboard focused on the assignee. Add the
matching `notification_route_q` entry so acknowledging that route marks the right
rows read.

---

## 14. A task can be created already overdue

**Low.**

`deadline` is a plain `DateField` (`models.py:61`) validated only for type by
`TaskWriteSerializer` (`serializers.py:65`). `create_task` does not compare it to
today (`services/tasks.py:35-76`), and `Task.clean()` checks only the
`assigned_by` invariant (`models.py:86-93`).

A task created with yesterday's deadline is `OVERDUE` the moment it exists
(`models.py:102-103`), and immediately counts against the assignee's overdue
number and their manager's area percentage.

That may be wanted - recording something already late is a legitimate act - but
it is currently indistinguishable from a typo, and there is nothing in the API to
say which it was. `PATCH` can move a deadline backwards too (§1), which makes a
task overdue retroactively with no record.

**The fix.** Either refuse a past deadline on create with a clear message, or
accept it deliberately and require an explicit flag. Whichever, say so in the
serializer.

---

## 15. Task creation tells a caller whether a user id exists

**Low.**

```python
# serializers.py:69-74
def validate_assignee_id(self, value):
    if value is None:
        return value
    if not User.objects.filter(pk=value).exists():
        raise serializers.ValidationError("No such user.")
    return value
```

versus, for an id that does exist but is out of the caller's area:

```python
# services/tasks.py:57-59
raise PermissionDenied("You can only assign tasks to people within your team.")
```

`400 "No such user."` for a free id; `403` about teams for a real one. The
existence check is unscoped - it spans every user on the platform, school
accounts included - so any CX staff member can walk the integer space and learn
which user ids are real.

The exposure is small (the caller is already trusted CX staff, and they learn an
id, not an identity). It is listed because it is the same shape as
`error/tickets/ticket_code_issues.md` §14 and both would be fixed by the same
habit.

**The fix.** Answer the same message for both cases, and resolve the assignee
from `TodoHierarchy.descendant_users(actor)` - the set the picker already
returns - rather than from `User.objects`.

---

## 16. Smaller defects and dead code

**Low, individually.**

1. **`update` and `destroy` resolve the object twice.** Both call
   `self.get_object()` (`views.py:118,129`) and then delegate to the mixin, which
   calls `get_object()` again (`core/mixins.py:62,91`). Each resolution runs
   `can_view_task`, which rebuilds the whole position index - so a `PATCH` walks
   the org tree twice before it writes anything.
2. **A person outside your area is a `403`; a task outside it is a `404`.**
   `_resolve_focus` and the `?assignee=` check both answer `403` with "That
   person is not in your team." (`views.py:45,69`), confirming the id belongs to
   somebody real, while `get_object` answers `404` and confirms nothing
   (`views.py:89`). Only the second is careful, and both were written for the
   same boundary.
3. **`school=None` is passed by an engine app.** `tasks.py:87` names the legacy
   parameter where `tenant=` is the current one
   (`vs_notifications/notify.py:52-61`). It works only because the dispatcher
   falls back to the first recipient's tenant
   (`vs_notifications/services/dispatch.py:118-122`), which happens to be the
   right answer here. Passing `tenant=reviewer.tenant` would make it intentional
   rather than lucky.
4. **`is_manager` is computed two ways on one screen** - `bool(reports)` for the
   focus node and a per-report `TodoHierarchy.is_manager` query for each card
   (`services/dashboards.py:30,35`).
5. **`MineView` returns raw stats** rather than `StatsSerializer` output
   (`views.py:165`), so the two representations of the same five numbers are kept
   in step by nothing.
6. **A vacant middle seat serializes as `"person": null`** with
   `is_manager: true` (`services/dashboards.py:73-82`). Deliberate - dropping it
   would orphan the branch below - but nothing in the payload names the empty
   seat, so the frontend can only draw a blank card.
7. **A multi-incumbent seat has one reviewer and one breadcrumb entry.**
   `_holders` is written to return every active holder
   (`services/hierarchy.py:57-63`), while `chain_to` and `direct_manager` take
   `Position.current_holder`, the single primary
   (`vs_user/models.py:1385-1404`).
8. **The department snapshot and the hierarchy read different columns.**
   `_department_for` goes through `PlatformStaffProfile.position`
   (`services/tasks.py:26-32`) while `primary_position` reads
   `PositionAssignment` (`services/hierarchy.py:27-36`). `OrganogramService`
   keeps them in step (`vs_user/services/organogram.py:80-81,94-96`); any write
   that bypasses it leaves a task labelled with the wrong department.
9. **`set_done` writes even when nothing changed** (`services/tasks.py:88`) -
   `mark_done`/`reopen` are no-ops on a task already in the target state, but the
   `save()` runs regardless and bumps `updated_at`.
10. **No throttling anywhere.** `DEFAULT_THROTTLE_CLASSES` is
    `ScopedRateThrottle` alone (`apps/settings/base.py:68-70`) and no `vs_todo`
    view declares a `throttle_scope`. Low risk on an internal tool, noted for
    completeness.
11. **`get_initials` on an account with no name** splits the email address
    (`serializers.py:29-31`), so `t.member@cx.test` yields `T` rather than
    anything meaningful.
12. **Going overdue is completely silent** - no sweep, no periodic task, no
    notification. The status simply starts answering differently on the day the
    deadline passes.
13. **`Task.clean()` runs only on create** (`services/tasks.py:74`). Harmless
    today, because its one invariant concerns `assigned_by`, which the update
    serializer does not expose - but §1 shows how thin that reasoning is.

---

## What the test suite does not know

The suite is green, so every item above is something it does not catch. The
single largest gap dwarfs the rest:

1. **There is no HTTP test in this module at all.** `tests.py` contains no
   `APIClient` and makes no request. Every one of the ten endpoints - the gate,
   the filters, the `403`s, the `404`, the envelopes, the pagination, the empty
   response shape - is untested. §1, §7, §8 and §11 all live in that gap, and so
   does any assurance that `IsVisionStaff` keeps a school account out.
2. **Nothing asserts ordering**, which is why §5 has survived.
3. **Nothing goes through `set_done` with `captureOnCommitCallbacks`**, so
   nothing pins that a completion queues a review request at all - the two
   dispatch tests call the Celery task directly (§4, and
   `todo_review_requests` §11).
4. **Nothing exercises the seatless person**, the vacant seat, the
   multi-incumbent seat, or a cycle - the four org shapes the hierarchy code is
   full of special handling for (§12).

What the suite *is* good at is worth saying: `HierarchyTests`
(`tests.py:81-114`) asserts `can_assign` in all four directions, and
`ReviewRequestDispatchTests` (`tests.py:183-243`) seeds the real notification
registry and templates rather than mocking them, and asserts the rendered subject
and body. Those two classes are the model the rest of the file should follow.
