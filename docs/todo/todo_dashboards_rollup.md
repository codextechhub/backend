# todo_dashboards_rollup

The three read-only screens and the picker behind the assign modal: "My Tasks",
"My Team" with its drill-down and breadcrumb, the organogram roll-up tree, and
the list of people a manager may assign to. This is where the tree
(`todo_hierarchy_scoping`) and the tasks (`todo_tasks`) are put together into
numbers.

Routes: `/v1/todo/dashboard/mine/`, `/dashboard/team/`, `/dashboard/org/`,
`/assignable/`.

---

## 1. What it is (and what it is NOT)

- **Three shapes, one vocabulary.** Every screen ultimately renders the same
  five numbers - `total`, `done`, `in_progress`, `overdue`, `pct` - produced by
  one function, `stats_for` (`services/stats.py:16`). The rings, pills and
  progress bars in the design are all that dict.
- **"Own" and "area" are different questions, always both asked.** Own is the
  tasks a person is personally accountable for; area is own plus everyone
  beneath them. A manager's dashboard shows both, side by side, because the
  design's point is that a manager is accountable twice.
- **Nothing here is stored or cached.** Every number is computed per request from
  live tasks and the live org tree. There is no nightly roll-up, no materialised
  counter and no cache key to invalidate.
- **These are not reports.** There is no date range, no export, no comparison
  against a previous period, and no history: `stats_for` counts the current state
  of whatever tasks exist, from the beginning of time.
- **None of them is paginated.** `dashboard/mine/` and `dashboard/team/` return
  every task the person has ever had, in full, and `dashboard/org/` walks the
  whole subtree in one response (§8).
- **`assignable/` is not the same list as the team dashboard's reports.** It is
  every descendant at any depth (`descendant_users`); the dashboard's `reports`
  are the one level directly below (`direct_report_users`).
- **The org tree can contain person-less nodes.** A vacant seat with reports
  underneath it is kept, serialized with `"person": null`, so the branch below it
  is not lost (§5).

## 2. Domain model

None. This slice owns no model. It reads `Task` (`todo_tasks` §2) and the
`vs_user` organogram (`todo_hierarchy_scoping` §1), and produces plain
dictionaries which the serializers shape.

The dictionaries are the contract, so they are worth writing out:

```python
stats           = {total, done, in_progress, overdue, pct}
report_card     = {person, is_manager, area_stats}
node_dashboard  = {person, is_manager, own_tasks, own_stats, area_stats,
                   reports: [report_card], breadcrumb: [person]}
org_rollup_node = {person, is_manager, own_stats, area_stats,
                   direct_reports: [org_rollup_node]}
```

`org_rollup_node` also carries a private `_area_tasks` key while the tree is
being built, popped before it is returned (`services/dashboards.py:76,91`).

## 3. Endpoint map

All four are `APIView`s with `permission_classes = TODO_PERMISSIONS`
(`views.py:33`) - an active account on a `PLATFORM` tenant, and no RBAC key. All
four require `?tenant=`, which in practice is always `codex`.

| Method + path | Query | Response |
|---|---|---|
| `GET /dashboard/mine/` | - | `{person, tasks, stats}` |
| `GET /dashboard/team/` | `focus=<user_id>` | `NodeDashboardSerializer` |
| `GET /dashboard/org/` | - | `OrgRollupNodeSerializer`, or `null` |
| `GET /assignable/` | - | `PersonSerializer[]` |

All four return the standard envelope with the message "Data retrieved
successfully.", and none is paginated.

`?focus=` defaults to the caller. A different person is allowed only if they are
inside the caller's area; otherwise `_resolve_focus` raises `403` "That person is
not in your team." (`views.py:36-46`). Note that this answers `403` rather than
`404`, so it confirms that the id belongs to a real person outside your team -
the opposite choice from `get_object` on a task (`todo_hierarchy_scoping` §5).

`/dashboard/org/` is rooted at the **caller**, not at the top of the company, so
what each person sees is their own subtree. The MD sees everything; a rep sees
one node.

### Serializer field sets

| Serializer | Fields |
|---|---|
| `StatsSerializer` (`serializers.py:84`) | `total`, `done`, `in_progress`, `overdue`, `pct` |
| `ReportCardSerializer` (`serializers.py:92`) | `person`, `is_manager`, `area_stats` |
| `NodeDashboardSerializer` (`serializers.py:100`) | `person`, `is_manager`, `own_tasks`, `own_stats`, `area_stats`, `reports`, `breadcrumb` |
| `OrgRollupNodeSerializer` (`serializers.py:111`) | `person`, `is_manager`, `own_stats`, `area_stats`, `direct_reports` |

`OrgRollupNodeSerializer` recurses into itself through a
`SerializerMethodField` (`serializers.py:119-120`), which is how an
arbitrarily deep tree is rendered by a flat serializer definition.

`MineView` does not use a serializer for its envelope; it assembles
`{person, tasks, stats}` by hand (`views.py:157-167`), so `stats` there is the
raw dict rather than `StatsSerializer` output. The keys are identical.

## 4. Lifecycle

None. Every endpoint here is a `GET` and writes nothing - no row, no audit
event, no read receipt. A manager can read every number about every person
beneath them, at any depth, and nothing anywhere records that they did.

## 5. Derivations

- **`stats_for`** (`services/stats.py:16-35`) iterates the tasks once, reading
  the derived `status` per row, and computes
  `pct = round(done / total * 100)`, or `0` for an empty set. It accepts any
  iterable, so it is fed querysets in some places and lists in others.
- **`own_tasks_qs(user)`** is `Task.objects.filter(assignee=user)`
  (`services/stats.py:38-40`); **`area_tasks_qs(user)`** is
  `assignee_id__in=area_user_ids(user)` (`services/stats.py:43-45`). Two lines,
  and every number on every screen comes from one of them.
- **`node_dashboard(focus)`** (`services/dashboards.py:25-43`) assembles the
  focus person's own tasks and stats, their area stats, a card per direct report
  carrying **that report's own area roll-up**, and the breadcrumb from
  `chain_to`. `is_manager` on the node is `bool(reports)` - derived from the
  cards just built - while `is_manager` inside each card comes from
  `TodoHierarchy.is_manager(report)`, a separate query. The two answers agree
  but are computed differently.
- **`org_rollup(root)`** (`services/dashboards.py:46-92`) is the careful one, and
  the pattern the rest of the module should follow:
  1. `_children_index()` once;
  2. **one** task fetch for the whole area, grouped into
     `{assignee_id: [tasks]}` (`services/dashboards.py:60-63`);
  3. a recursive `build(position)` that computes each node's own stats from that
     dict and accumulates its area tasks upward through the private
     `_area_tasks` key, so a node's area stats are its own tasks plus everything
     its children already gathered - no re-querying per node.
- **Empty branches are dropped**: `build` returns `None` for a vacant seat with
  no children (`services/dashboards.py:73-74`), so the tree does not fill with
  placeholder cards for unfilled roles at the leaves.
- **Vacant *middle* seats are kept**, with `"person": None` and
  `is_manager: True`. DRF renders a `None` nested serializer as `null` rather
  than failing, so the frontend receives a node it must be prepared to draw
  without a person.
- **A person with no seat still gets a tree**: `org_rollup` falls back to a
  single self node with own stats used for both own and area
  (`services/dashboards.py:48-55`).
- **`assignable/`** is `descendant_users(request.user)` (`views.py:211`) - every
  descendant at any depth, which is exactly the set `can_assign` will accept, so
  the picker and the rule cannot disagree.

## 6. What reading writes

Nothing at all, in any sense: no rows, no audit, no counters, no cache.

The cost, however, is not nothing. `node_dashboard` calls
`TodoHierarchy.is_manager(report)` and `area_tasks_qs(report)` per direct report
(`services/dashboards.py:29-33`), and each of those rebuilds
`_children_index()` from scratch. A head with eight direct reports therefore
scans the whole `Position` table around seventeen times and issues roughly forty
queries to draw one screen - while `org_rollup`, next door in the same file,
draws a whole tree with two. That contrast is `todo_code_issues.md` §9.

## 7. Worked example

Using the fixture tree (`tests.py:43-79`) with member: one done, one open;
head: one open:

```text
GET /v1/todo/dashboard/team/?tenant=codex        (as Chidi, Head of Sales)
```

```json
{ "success": true, "message": "Data retrieved successfully.",
  "data": {
    "person":     {"id": 22, "name": "Chidi Head", "role": "Head of Sales", "initials": "CH"},
    "is_manager": true,
    "own_tasks":  [ { "id": 3, "title": "h1", "status": "IN_PROGRESS", … } ],
    "own_stats":  {"total": 1, "done": 0, "in_progress": 1, "overdue": 0, "pct": 0},
    "area_stats": {"total": 3, "done": 1, "in_progress": 2, "overdue": 0, "pct": 33},
    "reports": [
      { "person": {"id": 41, "name": "Tobi Member", "role": "Sales Rep", "initials": "TM"},
        "is_manager": false,
        "area_stats": {"total": 2, "done": 1, "in_progress": 1, "overdue": 0, "pct": 50} } ],
    "breadcrumb": [
      {"id": 7,  "name": "Ada Director", "role": "Managing Director", "initials": "AD"},
      {"id": 22, "name": "Chidi Head",   "role": "Head of Sales",     "initials": "CH"} ] } }
```

`own_stats.total` is 1 and `area_stats.total` is 3: Chidi's one task plus Tobi's
two. `pct` is 33, not 0 - the area is a third done even though Chidi personally
has finished nothing, which is the number the design wants a manager to be
judged on.

Then the tree, as the MD:

```text
GET /v1/todo/dashboard/org/?tenant=codex         (as Ada)
```

returns a node for Ada (`area_stats.total` 3, `own_stats.total` 0) whose
`direct_reports` holds Chidi's node (area 3, own 1), whose `direct_reports` holds
Tobi's (area 2, own 2). Sola, the lone wolf reporting to nobody, appears in
nobody's tree.

And the picker:

```text
GET /v1/todo/assignable/?tenant=codex            (as Chidi)
  → [ {"id": 41, "name": "Tobi Member", "role": "Sales Rep", "initials": "TM"} ]
```

one entry, matching exactly what `can_assign` will let him do.

## 8. Gotchas / known limitations

Full evidence in **`error/todo/todo_code_issues.md`**.

- **`node_dashboard` rebuilds the position tree once per direct report**, while
  `org_rollup` in the same file builds it once for a whole tree
  (`services/dashboards.py:29-33` versus `46-63`)
  (`todo_code_issues.md` §9).
- **Every task ever, in one response.** `dashboard/mine/` and the `own_tasks` of
  `dashboard/team/` return the person's complete history with no window and no
  pagination (`views.py:159`, `services/dashboards.py:26`)
  (`todo_code_issues.md` §10).
- **`org_rollup`'s recursion has no cycle guard.** `build()` recurses through
  `_children_index` with no `seen` set, unlike `descendant_users` and `chain_to`
  which both carry one. `Position.clean()` forbids cycles, but `clean()` only
  runs from forms and serializers (`todo_code_issues.md` §12).
- **`?focus=abc` is a 500** - a bare `int()` on a query parameter
  (`views.py:44`) (`todo_code_issues.md` §7).
- **`?focus=` answers `403` for a real person outside your team**, confirming
  they exist, where the task routes answer `404` and confirm nothing.
- **A vacant middle seat serializes as `"person": null`** with
  `is_manager: true`. Deliberate - dropping it would orphan the branch - but the
  frontend has to handle a card with no person, and nothing in the payload
  labels which seat is empty.
- **`is_manager` is computed two ways** on the same screen:
  `bool(reports)` for the focus node and a separate `TodoHierarchy.is_manager`
  query per report card (`services/dashboards.py:30,35`).
- **`pct` is `round()`ed**, so 1 of 3 shows as 33 and 2 of 3 as 67; the two do not
  sum to 100 and never will. Worth knowing before somebody "fixes" it.
- **`MineView` returns raw stats** rather than `StatsSerializer` output
  (`views.py:165`). Same keys today; nothing keeps them in step.
- **Nothing records that a manager read a report's numbers.**
- **Justified by design:** the tree is rooted at the caller, so the org screen is
  everyone's own subtree rather than a company-wide directory.
- **Justified by design:** `assignable/` and `can_assign` read the same function,
  so the picker cannot offer somebody the service will then refuse.

## 9. Permissions & tenant isolation

| Surface | Gate | Scope of what comes back |
|---|---|---|
| `dashboard/mine/` | `IsVisionStaff` | the caller alone |
| `dashboard/team/` | + `_resolve_focus` | the caller, or one person inside their area |
| `dashboard/org/` | `IsVisionStaff` | the caller's subtree |
| `assignable/` | `IsVisionStaff` | the caller's descendants |

Every scope is derived from the caller's own seat, so there is no parameter that
widens any of them except `?focus=`, which is bounded by `area_user_ids`.

There is one thing worth stating plainly: **these screens are a personnel
surface.** `PersonSerializer` exposes name, role and initials for everybody in a
manager's subtree, and the roll-up exposes each of them by completion
percentage. That is the tool's purpose, and the boundary that keeps it
appropriate is the organogram - not a permission key, because there is none
(`todo_hierarchy_scoping` §2).

Tenant isolation does not arise: every row and every person here is on the
platform tenant, and `IsVisionStaff` is what keeps a school account out.

## 10. Code map

| File | Responsibility |
|---|---|
| `services/stats.py:16-35` | `stats_for` - the five numbers |
| `services/stats.py:38-45` | `own_tasks_qs`, `area_tasks_qs` |
| `services/dashboards.py:25-43` | `node_dashboard` - the My Team payload |
| `services/dashboards.py:46-92` | `org_rollup` - the single-pass tree |
| `views.py:150-167` | `MineView` |
| `views.py:170-183` | `TeamView` |
| `views.py:186-198` | `OrgView` |
| `views.py:201-215` | `AssignableView` |
| `views.py:36-46` | `_resolve_focus` - the `?focus=` bound |
| `serializers.py:84-120` | `StatsSerializer`, `ReportCardSerializer`, `NodeDashboardSerializer`, `OrgRollupNodeSerializer` |
| `services/hierarchy.py` | Everything the three screens ask about the tree |

## 11. Test coverage & gaps

- `TaskStatusTests.test_stats_for_counts_and_pct` (`tests.py:136-145`) - the
  counts and the percentage.
- `DashboardTests.test_area_tasks_roll_up` (`tests.py:254-258`) - a head's area
  and the MD's area both total three.
- `DashboardTests.test_node_dashboard_shape` (`tests.py:260-266`) -
  `is_manager`, own versus area totals, and the report ids.
- `DashboardTests.test_org_rollup_tree` (`tests.py:268-275`) - the root, its area
  total, and the first child's node.

What the suite does not cover:

1. **The HTTP surface.** None of the four endpoints is called in a test; there is
   no `APIClient` in the file. The envelope, the `?focus=` bound, the `403`, and
   the `null` body from `/dashboard/org/` for a seatless caller are all
   unexercised.
2. **`_resolve_focus`** in any branch.
3. **`assignable/`** - `descendant_users` is tested, the endpoint is not.
4. **A vacant seat**, at a leaf (dropped) or in the middle (kept with a null
   person). Both branches of `build`'s pruning rule are unasserted.
5. **A seatless caller** through `org_rollup`'s fallback node.
6. **`breadcrumb`** in a dashboard payload - `chain_to` is tested directly, but
   nothing asserts it reaches the response.
7. **Overdue tasks in a roll-up.** Every fixture task uses today's date, so
   `stats_for`'s `overdue` branch is zero in every dashboard test.
8. **Depth beyond three levels**, and a manager with more than one direct report.
