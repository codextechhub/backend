# todo_hierarchy_scoping

The tree. How `vs_todo` turns the CX organogram into the four answers it needs -
who is beneath me, who reports to me directly, who may I assign to, and who is
above me - and how those answers become the module's only access boundary. The
task itself is `todo_tasks`; the screens built on this are
`todo_dashboards_rollup`.

Everything here lives in `services/hierarchy.py` (one class,
`TodoHierarchy`), plus the two predicates in `services/tasks.py` and the
permission list in `views.py`.

---

## 1. What it is (and what it is NOT)

- **The hierarchy is not stored.** No column on `Task` records a manager, a team
  or a depth. Every question is answered live from
  `vs_user.Position.reports_to` and `PositionAssignment`
  (`services/hierarchy.py:1-16`), so the ToDo tree always matches the current org
  chart and a reorganisation needs no data migration here.
- **The tree is seats, not people.** Reporting lines run position → position
  (`vs_user/models.py:1335-1339`). People are attached to seats by
  `PositionAssignment`, and this module's whole job is translating the first into
  the second.
- **It is the CX organogram, and only that.** `OrgNode`, `Position` and
  `PositionAssignment` carry no tenant column
  (`vs_user/models.py:1164-1467`), and `OrganogramService.assign_to_position`
  refuses a non-platform user outright
  (`vs_user/services/organogram.py:56-60`). There is no way for a school user to
  appear in this tree, which is what makes the unfiltered
  `Position.objects.filter(is_active=True)` at `services/hierarchy.py:46-50`
  safe.
- **"Your area" includes you.** `area_user_ids` is descendants **plus** the
  viewer (`services/hierarchy.py:111-116`). "Descendants" never does.
- **Access is structural, not granted.** There is no RBAC key in the request
  path. The module gates on `IsVisionStaff` and then lets the tree decide what
  you see (§3).
- **A person with no seat is an island.** `primary_position` returns `None`, so
  `descendant_users` is `[]`, `is_manager` is `False`, `can_assign` is always
  `False`, and `area_user_ids` is `{self}`. They see and manage their own tasks
  and nothing else. Nothing errors.
- **Multi-incumbent seats are handled; the breadcrumb is not.**
  `_holders` returns every active holder of a seat
  (`services/hierarchy.py:57-63`), but `chain_to` and `direct_manager` take
  `Position.current_holder`, which is the single primary holder
  (`vs_user/models.py:1385-1404`).

## 2. The three keys that do nothing

`constants.py:35-38` declares them and says so:

```python
# These keys exist for future fine-grained wiring through the RBAC registry;
# today the views gate on CX-staff membership.
PERM_TASK_VIEW   = "todo.task.view"
PERM_TASK_MANAGE = "todo.task.manage"
PERM_TASK_ASSIGN = "todo.task.assign"
```

`manage.py seed_todo_permissions` registers all three under a `todo` permission
module and grants them to `xvs_super_admin` and `xvs_platform_admin` on the
`codex` tenant (`management/commands/seed_todo_permissions.py:15-25,95-129`).
All three are `PermissionScope.TENANT`, `is_restricted=False`, sensitivity
`NORMAL`.

No view sets `rbac_permission`. No service calls `user_has_rbac_permission`.
Grepping the module for any of the three keys finds only their declaration and
the seeder. They are, today, three rows in a table (`todo_code_issues.md` §11).

## 3. The gate

```python
# views.py:33
TODO_PERMISSIONS = [IsAuthenticatedAndActive & IsVisionStaff]
```

- **`IsAuthenticatedAndActive`** (`vs_rbac/permissions.py:204-229`) - a live
  account (not SUSPENDED, LOCKED or DEACTIVATED) and `TenantSurfaceAllowed`,
  which refuses a PENDING tenant anything outside its declared surface. No
  `vs_todo` view declares `pending_tenant_surface`, and the platform tenant is
  never PENDING, so that clause never fires here.
- **`IsVisionStaff`** (`vs_rbac/permissions.py:233-244`) - `user.tenant.kind ==
  "PLATFORM"`. Nothing else. Not a role, not a key, not a staff profile, not an
  employment status.

So the tool is open to **every active account on the platform tenant**: a
support agent, a consultant, an intern, an account created for a contractor.
What each of them can then *do* is bounded entirely by the tree, and a person
with no seat gets an empty, harmless tool. But the coarseness is real, and it is
the reason §2's keys were written down in the first place.

## 4. The four questions

All four start from `primary_position(user)` - the user's open, primary
`PositionAssignment` (`services/hierarchy.py:27-36`) - and all but that one build
`_children_index()`, a single pass over active positions producing
`{reports_to_id: [child positions]}` (`services/hierarchy.py:38-53`). The index
is rebuilt per call, which the docstring justifies (one seat per role, small
table) and which §8 revisits.

### `descendant_users(user)` (`services/hierarchy.py:65-93`)

Everyone strictly beneath the user. An explicit stack walk down the index,
carrying two `seen` sets - one for positions, one for users - so a cycle in the
seat graph cannot loop forever and a person holding two seats in the subtree
appears once. The user is pre-seeded into `seen_users`, so they are never
returned even if they hold one of their own descendant seats.

### `direct_report_users(user)` (`services/hierarchy.py:95-109`)

One level: the holders of the seats whose `reports_to` is the user's seat.

### `can_assign(manager, target)` (`services/hierarchy.py:126-131`)

`False` for yourself, otherwise "is the target among my descendants". This is the
whole of the assign-down rule, and `create_task` is its only caller
(`services/tasks.py:56-59`).

### `chain_to(user)` and `direct_manager(user)`

Two walks **up** the same tree, with deliberately different stopping rules:

| | Walks past a vacant seat? | Used by |
|---|---|---|
| `chain_to` (`services/hierarchy.py:153-174`) | **No** - stops, "the chain is only as deep as it is filled" | the team dashboard breadcrumb |
| `direct_manager` (`services/hierarchy.py:133-151`) | **Yes** - "so a missing middle manager doesn't swallow escalations" | the review request's reviewer fallback |

Both carry a `seen_positions` set. `direct_manager` additionally skips a holder
who is the user themselves, so somebody occupying their own manager's seat does
not become their own reviewer.

The asymmetry is intentional and documented in both docstrings, but it does mean
the breadcrumb can be shorter than the escalation path: a vacant Head of Sales
truncates Tobi's breadcrumb to himself, while his review requests still reach the
MD above the gap.

## 5. From the tree to the boundary

Two predicates in `services/tasks.py` translate the tree into permission:

```python
def can_view_task(viewer, task):      # :121
    return task.assignee_id in TodoHierarchy.area_user_ids(viewer)

def can_modify_task(viewer, task):    # :126
    if task.assignee_id == viewer.pk:
        return True
    return TodoHierarchy.can_assign(viewer, task.assignee)
```

and the views apply them:

| Surface | Check | Failure |
|---|---|---|
| `GET /tasks/` with `?assignee=` | `int(id) in area_user_ids(viewer)` (`views.py:68`) | `403` "That person is not in your team." |
| `GET/PUT/PATCH/DELETE /tasks/<pk>/`, `toggle` | `can_view_task` in `get_object` (`views.py:88`) | `404` "No such task." |
| `PUT/PATCH`, `DELETE`, `toggle` | `can_modify_task` (`views.py:119,130,137`) | `403` |
| `POST /tasks/` with `assignee_id` | `can_assign` in `create_task` | `403` |
| `dashboard/team/?focus=` | `_resolve_focus` (`views.py:36-46`) | `403` |
| `assignable/` | returns `descendant_users` | - |

**View is area-wide; modify is strictly downward.** `can_view_task` uses
`area_user_ids`, which includes the viewer; `can_modify_task` uses `can_assign`,
which excludes them. The one place the two meet is the assignee themselves, who
is admitted by the first branch of `can_modify_task`.

**Notice what `can_modify_task` grants.** Anybody above you in the chain, at any
depth, may edit, complete, reopen and *delete* your tasks - the MD may delete a
task Tobi set for himself three levels down. That is a defensible reading of a
management tool, but combined with the absence of any audit trail it is
`todo_code_issues.md` §2.

**Notice the `403` versus `404` split.** Asking for a *person* outside your area
answers `403` and confirms they exist; asking for a *task* outside your area
answers `404` and confirms nothing. The two are inconsistent, and only the second
one is careful.

## 6. What the boundary writes

Nothing. Every function in `services/hierarchy.py` is a read, and so are the two
predicates. There is no cache, no memoisation and no per-request store: two calls
in the same request rebuild the index twice.

There is also no record of a refusal. A `403` from `_resolve_focus`, a `404` from
`get_object` and a rejected assignment leave no trace anywhere - this module
writes no audit events at all.

## 7. Worked example

The fixture tree in `tests.py:43-79`, which is the shape to reason with:

```text
Executive (division)
└── Managing Director  ── Ada Director
    └── Head of Sales  ── Chidi Head          (Sales department)
        └── Sales Rep  ── Tobi Member
Executive (division)
└── Lone Wolf          ── Sola Outsider       (reports to nobody)
```

| Question | Ada (MD) | Chidi (Head) | Tobi (Rep) | Sola (Lone Wolf) |
|---|---|---|---|---|
| `descendant_users` | Chidi, Tobi | Tobi | - | - |
| `direct_report_users` | Chidi | Tobi | - | - |
| `area_user_ids` | Ada, Chidi, Tobi | Chidi, Tobi | Tobi | Sola |
| `is_manager` | yes | yes | no | no |
| `chain_to` | [Ada] | [Ada, Chidi] | [Ada, Chidi, Tobi] | [Sola] |

So:

- Chidi may assign to Tobi, and may not assign to Ada (up), to Sola (sideways)
  or to himself. All four are pinned at `tests.py:104-110`.
- Ada may open, edit and delete any of Tobi's tasks, two levels down.
- Sola sits in the same division as Ada but under no seat of hers, so Ada cannot
  see a single one of Sola's tasks, and Sola sees only her own. A "lone wolf"
  reporting to nobody is invisible to the entire roll-up.
- If Chidi's seat is vacated, Tobi's breadcrumb becomes `[Tobi]` while his
  completion review requests start reaching Ada.

## 8. Gotchas / known limitations

Full evidence in **`error/todo/todo_code_issues.md`**.

- **Three seeded RBAC keys are checked nowhere, and the gate is "any platform
  account"** (`constants.py:35-38`, `views.py:33`). Every CX account gets the
  tool (`todo_code_issues.md` §11).
- **The position tree is rebuilt on every question.** `_children_index()` is one
  query plus a prefetch, and the team dashboard calls the functions that build it
  four or more times per direct report (`todo_code_issues.md` §9).
- **`?focus=abc` and `?assignee=abc` are 500s.** Both do a bare `int()` on a
  query parameter (`views.py:44,68`) (`todo_code_issues.md` §7).
- **A person outside your area is a `403`, while a task outside it is a `404`**
  (`views.py:45,69` versus `views.py:89`). The careful answer is only given on
  one of the two.
- **`org_rollup` recurses with no cycle guard**, while `descendant_users` and
  `chain_to` in this same file both carry one
  (`services/dashboards.py:66-87` versus `services/hierarchy.py:79-80,165`)
  (`todo_code_issues.md` §12).
- **`can_modify_task` is delete as well as edit**, and nothing is audited
  (`todo_code_issues.md` §2).
- **The department snapshot and the hierarchy read two different columns.**
  `_department_for` goes through `PlatformStaffProfile.position`
  (`services/tasks.py:26-32`) while `primary_position` reads
  `PositionAssignment` (`services/hierarchy.py:27-36`). `OrganogramService`
  keeps them in step (`vs_user/services/organogram.py:80-81,94-96`); anything
  writing an assignment without going through it does not
  (`todo_code_issues.md` §16 item 8).
- **A multi-incumbent seat has one breadcrumb.** `chain_to` and `direct_manager`
  take `current_holder`, the single primary, while `_holders` correctly returns
  everyone. Two people jointly holding a seat produce one reviewer and one
  breadcrumb entry.
- **Justified by design:** the tree is derived live rather than denormalized onto
  the task, so a reorganisation is immediately reflected everywhere.
- **Justified by design:** the unfiltered `Position.objects` query is safe
  because the organogram is structurally CX-only.
- **Justified by design:** `chain_to` stops at a vacant seat while
  `direct_manager` walks past one - both docstrings say why.

## 9. Permissions & tenant isolation

| Layer | What it answers |
|---|---|
| `IsAuthenticatedAndActive` | Is this a live account, on a live tenant? |
| `IsVisionStaff` | Is that tenant `PLATFORM`? |
| `area_user_ids` / `can_view_task` | May you *see* this person's tasks? |
| `can_assign` / `can_modify_task` | May you *change* them? |

There is no tenant isolation problem to solve inside the module: one tenant owns
every row. The isolation that matters is the boundary between this tool and the
rest of the platform, and it is held by `IsVisionStaff` alone. A school account
cannot reach any `/v1/todo/` route, and cannot appear in the organogram that the
routes read.

The `?tenant=` parameter is still required by `TenantJWTAuthentication`
(`vs_rbac/authentication.py:128-131`), and a platform user asserting another
tenant's slug is refused there (119-127) because no view here opts into
`platform_cross_tenant_param`.

## 10. Code map

| File | Responsibility |
|---|---|
| `services/hierarchy.py:27-53` | `primary_position`, `_children_index` - the two lookups everything else builds on |
| `services/hierarchy.py:57-109` | `_holders`, `descendant_users`, `direct_report_users` |
| `services/hierarchy.py:111-131` | `area_user_ids`, `is_manager`, `can_assign` |
| `services/hierarchy.py:133-174` | `direct_manager`, `chain_to` - the two upward walks |
| `services/tasks.py:121-130` | `can_view_task`, `can_modify_task` |
| `views.py:33` | `TODO_PERMISSIONS` |
| `views.py:36-46` | `_resolve_focus` |
| `constants.py:35-38` | The three unused keys |
| `management/commands/seed_todo_permissions.py` | Registration and the platform grants |
| `vs_user/models.py:1164-1467` | `OrgNode`, `Position`, `PositionAssignment` - the tree this module reads |
| `vs_user/services/organogram.py` | The only writer of that tree, and the profile-position sync |

## 11. Test coverage & gaps

- `HierarchyTests` (`tests.py:81-114`) is the strongest part of the suite:
  descendants roll up the whole subtree, direct reports are one level only, area
  includes self, `is_manager` at three depths, and `can_assign` asserted in all
  four directions (down, up, sideways, self) plus the breadcrumb order.
- `TaskServiceTests.test_assignment_upward_is_rejected` (`tests.py:167-171`)
  pins the refusal at the service layer.
- `PermissionSeedTests.test_todo_seed_captures_and_grants_task_keys`
  (`tests.py:323-331`) - the seeder registers and grants the three keys.

What the suite does not cover:

1. **`IsVisionStaff`.** Nothing asserts that a school account is refused, because
   nothing in the suite makes an HTTP request at all.
2. **`can_view_task` and `can_modify_task`** directly - a peer opening a task
   outside their area, a report trying to edit their manager's task, the MD
   deleting a grandchild's task.
3. **A person with no seat** - the `primary_position is None` branch runs through
   five functions and is asserted in none of them.
4. **A vacant middle seat** - neither the truncated `chain_to` nor
   `direct_manager` walking past it.
5. **A multi-incumbent seat**, though `_holders` was written for one.
6. **A cycle in the seat graph**, which `descendant_users` guards against and
   `org_rollup` does not.
7. **`_resolve_focus`** in any of its three branches.
