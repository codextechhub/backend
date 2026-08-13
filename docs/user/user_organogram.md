# user_organogram

The internal org chart and the HR profile behind it: **staff profiles, the
Division/Department/Team tree, seats and who occupies them, dotted-line reporting,
and the four climb modes the approval engine uses to find a manager**. Routes are
at `/v1/user/platform-staff-profiles/` and `/v1/user/organogram/`.

---

## 1. What it is (and what it is NOT)

- `PlatformStaffProfile` is the HR record for a CX (platform) staff member: person,
  contact, next of kin, employment, and payroll. Kept off the `User` row so the
  auth model stays lean (`models.py:699-707`).
- `OrgNode` is the org tree, strictly tiered `DIVISION -> DEPARTMENT -> TEAM`
  (`models.py:871-1023`).
- `Position` is a **seat**, not a person. The solid reporting line is seat to seat
  through `reports_to` (`models.py:1026-1132`).
- `PositionAssignment` is an effective-dated tenure of a user in a seat, with full
  history: `end_date IS NULL` means current (`models.py:1135-1198`).
- `MatrixReport` is a dotted line between two seats, separate from the solid line
  (`models.py:1201-1235`).

**CX-only by design.** There are no school or branch fields anywhere in this
slice; school org charts are explicitly out of scope and reserved for a future
`staff` app (`models.py:699-707,865-869`). `PositionAssignment.clean()` and
`OrganogramService.assign_position` both refuse a non-CX user
(`models.py:1177-1180`; `services/organogram.py:56-60`).

**This grants no permissions.** `Position.default_role` exists but nothing reads
it - see §8.

## 2. Domain model

| Model | Key fields | Rules |
|---|---|---|
| `PlatformStaffProfile` | personal, contact, next-of-kin, `employee_id`, `job_title`, `position`, `employment_type`, `employment_status`, `date_joined`, `date_exited`, bank fields | OneToOne with the user, cascade; `employee_id` globally unique; CX-only, enforced in `clean()` because `user_type` lives on another table; indexed on `(position, employment_status)` and `employee_id` (`models.py:728-790`) |
| `OrgNode` | `name`, `code`, `kind`, `parent`, `head_position`, `description`, `is_active` | `code` unique and tier-prefixed; `parent` PROTECT; partial unique on `(name, parent)` and on `name` where `parent IS NULL`; tiering, self-parent and cycles rejected in `clean()` (`models.py:897-983`) |
| `Position` | `title`, `code`, `org_node`, `reports_to`, `default_role`, `headcount`, `is_active` | `code` unique; `org_node` PROTECT; `reports_to` SET_NULL with a cycle guard; indexed on `(org_node, is_active)`, `reports_to`, `code` (`models.py:1033-1076`) |
| `PositionAssignment` | `user`, `position`, `is_primary`, `is_acting`, `start_date`, `end_date` | `position` PROTECT, user cascade; "one current primary per user" enforced in `clean()` and the service, not by a constraint, because MariaDB cannot express it (`models.py:1145-1194`) |
| `MatrixReport` | `position`, `reports_to`, `relationship_label` | `unique_together (position, reports_to)`; self-reference rejected in `clean()` (`models.py:1208-1232`) |

The `parent` foreign key is `PROTECT` rather than `SET_NULL` on purpose: orphaning
a child would leave a parentless Department or Team, a state `clean()` forbids, so
the row would be neither editable nor deletable and would vanish from every
roll-up. The API answers `409` with the blocking counts instead
(`models.py:901-911`; `core/exceptions.py:96-114`).

## 3. Endpoint map

| Method + path | permission key | request body / query | response |
|---|---|---|---|
| `GET /platform-staff-profiles/` | any active user | Query `user`, `org_node` (pk or code), `position`, `employment_status`, `employment_type`, `search` (<=64 chars, per-word) | Paginated slim rows, no payroll (`views/organogram.py:99-156`) |
| `POST /platform-staff-profiles/` | `platform.staff_profile.create` | `user_id`, `position_id?`, any profile field; payroll fields need the payroll key | `201` enveloped profile |
| `GET /platform-staff-profiles/<pk>/` | any active user | - | Full profile for the owner, a super admin, or a holder of `platform.staff_profile.view`; otherwise the brief work-only shape (`views/organogram.py:158-180`) |
| `PATCH /platform-staff-profiles/<pk>/` | `platform.staff_profile.update` | Any profile field; payroll fields gated by FLS write | Enveloped profile |
| `GET|PATCH /platform-staff-profiles/me/` | any active user, platform tenant only | Any profile field | Own profile, created on first read; `404` for a non-platform tenant (`views/organogram.py:182-205`) |
| `GET /platform-staff-profiles/photos/` | any active user | - | `{user_id: absolute_photo_url}` for the current tenant (`views/organogram.py:207-229`) |
| `GET /organogram/nodes/` | active platform staff (`IsVisionStaff`) | Query `is_active`, `kind`, `parent`, `roots`, `search` | Paginated nodes with `head` and `children_count` (`views/organogram.py:256-288`) |
| `POST|PATCH|DELETE /organogram/nodes/<pk>/` | `platform.organogram.manage` | `name`, `code`, `kind`, `parent_id?`, `head_position_id?`, `description?`, `is_active?` | Enveloped node; `409` when a delete is blocked |
| `GET /organogram/positions/` | active platform staff | Query `org_node`, `reports_to`, `is_active`, `search`, `ordering` | Paginated seats with `current_holders`, `is_vacant`, `open_seats` (`views/organogram.py:315-352`) |
| `GET /organogram/positions/tree/` | active platform staff | Query `root?` | Nested solid-line tree (`views/organogram.py:354-368`) |
| `GET /organogram/positions/vacancies/` | `platform.organogram.view` | - | Active seats with at least one open seat (`views/organogram.py:370-375`) |
| `POST|PATCH|DELETE /organogram/positions/<pk>/` | `platform.organogram.manage` | `title`, `code`, `org_node_id`, `reports_to_id?`, `default_role?`, `headcount?`, `is_active?` | Enveloped seat |
| `GET /organogram/assignments/` | `platform.staff_profile.view` | Query `user`, `position`, `current` | Paginated assignments (`views/organogram.py:404-420`) |
| `GET /organogram/assignments/mine/` | any active user | - | The caller's own tenure history (`views/organogram.py:422-439`) |
| `GET /organogram/assignments/current/` | active platform staff | - | `{user, position, is_acting}` only, no dates or history (`views/organogram.py:441-467`) |
| `POST /organogram/assignments/` | `platform.organogram.manage` | `user_id`, `position_id`, `is_primary?`, `is_acting?`, `start_date?` | `201` via `OrganogramService.assign_position` (`views/organogram.py:469-492`) |
| `POST /organogram/assignments/<pk>/close/` | `platform.organogram.manage` | `end_date?` | Enveloped closed assignment (`views/organogram.py:494-501`) |
| `GET|POST|PATCH|DELETE /organogram/matrix-reports/` | read: active platform staff; write: `platform.organogram.manage` | `position_id`, `reports_to_id`, `relationship_label?` | Paginated / enveloped dotted lines (`views/organogram.py:504-531`) |

Payroll fields (`bank_name`, `account_name`, `account_number`) are stripped on
read without `platform.staff_payroll.view` and rejected on write without
`platform.staff_payroll.manage`, at the serializer layer so the gate holds on
every endpoint. The profile's owner is an explicit exception and can always read
and write their own (`serializers.py:696-806`).

## 4. Lifecycle / state machine

```text
OrgNode:   DIVISION (top level, no parent)
              └─ DEPARTMENT (parent must be a DIVISION)
                    └─ TEAM (parent must be a DEPARTMENT)
           is_active toggles availability; delete is blocked (409) while children
           or positions still point at the node

Position:  active ⇄ inactive;  reports_to = the solid line;  headcount = seats
           delete blocked (409) while assignments exist (PROTECT)

Assignment: assign_position(is_primary=True)
              ├─ close the user's current primary  (end_date = new start_date)
              ├─ create the new tenure
              └─ sync profile.position + profile.job_title
            close()  ─▶ end_date set;  if it was primary, profile cache cleared
            (history rows are never deleted)
```

Approval-engine climb modes, all resolved from the user's **current primary**
seat and all excluding the requester themselves
(`services/organogram.py:209-257`; consumed at
`vs_workflow/services/approvers.py:293-311`):

| Mode | Resolution |
|---|---|
| `DIRECT_MANAGER` | Holders of `position.reports_to` |
| `N_LEVELS_UP` | Holders of the seat `N` steps up, clamped to the top of the chain |
| `DEPARTMENT_HEAD` | Walks Team -> Department -> Division until a node has a filled `head_position` |
| `SPECIFIC_POSITION` | Holders of an explicitly named seat |

## 5. Derivations

- **Node code** is rewritten on every save as `DV-`/`DT-`/`TM-` plus the bare
  code, stripping any existing tier prefix first so re-saves and tier changes stay
  clean (`models.py:1011-1020`). A lookup by the bare code therefore never matches
  (`management/commands/seed_organogram.py:117-124`).
- **"Current holder"** means an open assignment held by an **active** user. A seat
  reserved for a hire who is still pending approval or activation is deliberately
  not counted as occupied, so such a hire never shows as a holder and never blocks
  headcount (`models.py:1078-1122`).
- **`current_holder`** prefers the primary open assignment and falls back to any
  open one; `open_seats` is `max(headcount - filled, 0)` (`models.py:1092-1129`).
- **`department` / `division`** on a profile are `nearest_of_kind` walking up from
  the seat's node, so someone sitting on a Team resolves to that Team's Department
  and Division (`models.py:809-827`; `models.py:999-1009`).
- **`current_line_manager`** is the holder of the cached position's `reports_to`
  seat, or `None` when there is no seat, no parent, or the parent is vacant
  (`models.py:839-848`).
- **Tree roots** are seats with no manager seat **or** whose manager seat is
  inactive or removed. Without the second case an inactive parent would silently
  drop its whole subtree from the chart (`services/organogram.py:167-172`).
- **List occupancy** is served from one prefetch of current assignments per
  request, ordered primary-first, which is what took the node and position lists
  off a 3-queries-per-row N+1 (`views/organogram.py:260-276,317-333`;
  `serializers.py:908-924,972-987`).
- **Profile search** applies each word independently so "Ada Lovelace" matches
  across `first_name` and `last_name`; every word must match at least one
  chart-safe field (`views/organogram.py:141-154`).

## 6. What posting does to the ledger

Nothing here posts. The durable effects are the tree rows, the seat rows, the
effective-dated assignment history, and two cached fields on the profile
(`position` and `job_title`) that `_sync_profile_position` keeps in step with the
current primary seat (`services/organogram.py:100-116`).

History is append-only in practice: `assign_position` closes the previous primary
by setting `end_date`, never by deleting it, and `end_assignment` is a no-op on an
already-closed row (`services/organogram.py:64-97`).

Two other slices write here. Creating a CX hire with a seat writes the primary
assignment immediately, and a rejected, withdrawn or cancelled creation workflow
closes every open assignment so the seat is vacated
(`services/user.py:120-135`; `workflow_handlers.py:61-81`).

## 7. Worked example

```json
POST /v1/user/organogram/assignments/?tenant=codex
{ "user_id": 42, "position_id": 7, "is_primary": true, "is_acting": true }
```

The service closes user 42's existing primary tenure with `end_date` equal to
today, writes the new row with `start_date` today, and syncs the profile's
`position` and `job_title` to seat 7 (`services/organogram.py:34-83`). The seat's
`open_seats` drops by one only if user 42 is active. Their department, division
and line manager all move at once because every one of them is derived from that
single primary seat, not stored.

`GET /organogram/assignments/current/` then shows
`{"user": {...}, "position": {...}, "is_acting": true}` and nothing else - tenure
dates and historical rows stay behind `platform.staff_profile.view`
(`views/organogram.py:441-467`).

## 8. Gotchas / known limitations

- **Updating an assignment bypasses every invariant.** `PositionAssignmentSerializer`
  is the only one of the four organogram serializers that does not call the
  model's `clean()`, and DRF never calls `full_clean()`, so
  `PATCH /organogram/assignments/<pk>/ {"is_primary": true, "end_date": null}`
  can give a user two current primary seats, or set an `end_date` before the
  `start_date` (`serializers.py:1002-1028` against `:926-936,989-999,1062-1072`).
  Creation is safe because it routes through the service
  (`views/organogram.py:469-492`); the update path does not.
- **The profile's `position_id` is directly writable.** Setting it moves the
  cached seat, and with it the derived department, division and line manager,
  without writing any `PositionAssignment` row - so the chart and the history
  disagree. The serializer's own comment says to prefer `OrganogramService`
  (`serializers.py:813-820`).
- **`/positions/vacancies/` and `/positions/tree/` are N+1.** `vacancies()` runs a
  count per active seat, and the serializer then re-queries current assignments
  three times per seat because `_current_users` is called once per field and the
  prefetch the list view sets up is absent here (`services/organogram.py:197-205`;
  `serializers.py:975-987`). `build_tree` prefetches `assignments__user` but then
  reads `current_holders`/`is_vacant`, which apply a different filter and so
  ignore the prefetch (`services/organogram.py:158-186`).
- **`Position.default_role` is dead weight.** It is stored, selected, serialized
  and writable, and nothing anywhere reads it - assigning someone to a seat grants
  no role (`models.py:1047-1052`; `serializers.py:952-955`;
  `views/organogram.py:328`). It is also a `PrimaryKeyRelatedField` over every
  tenant's role templates, so a platform seat can be pointed at a school tenant's
  role.
- **Deleting a manager seat silently flattens the chart.** `reports_to` is
  `SET_NULL`, so every direct report becomes a root rather than re-parenting to
  the grandparent seat (`models.py:1042-1046`). The same applies to
  `OrgNode.head_position`, where a deleted seat quietly leaves the node headless
  and `DEPARTMENT_HEAD` resolution walks past it (`models.py:914-918`;
  `services/organogram.py:229-247`).
- **`/platform-staff-profiles/me/` will create a profile for any platform-tenant
  user.** `get_or_create` bypasses `PlatformStaffProfile.clean()`, so the CX-only
  rule is not enforced on that path (`views/organogram.py:191-193` against
  `models.py:792-797`).
- **Assignment reads are gated by an HR key, not by platform membership.**
  `list`/`retrieve` require `platform.staff_profile.view` with no `IsVisionStaff`
  companion, unlike the node, position and matrix reads
  (`views/organogram.py:391-402`). Any tenant role granted that platform-namespaced
  key would see the platform's assignment history.
- **Justified by design:** the whole chart is readable by any active platform
  employee. Structure and occupancy are treated as company-public; personal, HR
  and payroll data are not, which is what the brief profile shape exists for
  (`views/organogram.py:249-254,305-313`; `serializers.py:741-769`).
- **Justified by design:** tier and cycle rules are enforced in `clean()` and
  re-run by the serializers, with the database carrying only the two partial name
  constraints. Direct ORM writers must reuse that boundary
  (`models.py:946-983`; `serializers.py:926-936`).
- **Justified by design:** DRF's automatic handling of the two conditional name
  constraints is disabled (`validators = []` plus an empty validator list on
  `name`) because it turns them into an unconditional field validator and makes
  the optional `parent_id` required; `OrgNode.clean()` does the parent-scoped check
  and the database constraints remain the concurrency-safe guard
  (`serializers.py:900-906`).

## 9. Permissions & tenant isolation

Three tiers, deliberately different (`views/organogram.py:86-97,249-254,305-313,
391-402,513-518`):

1. **Chart reads** - nodes, positions, tree, matrix lines, current assignments,
   profile list and photos: any active user, with `IsVisionStaff` (tenant kind
   `PLATFORM`) on everything except the profile list, retrieve, `me` and `photos`.
2. **HR reads and writes** - full profile and assignment history:
   `platform.staff_profile.view` / `.create` / `.update`, with the owner always
   allowed to see their own.
3. **Structure writes and summary metrics** - `platform.organogram.manage` for
   every write, `platform.organogram.view` for `vacancies`.

Profiles are scoped to `request.tenant` (falling back to the caller's own tenant)
on the list, and `photos` is scoped the same way, so a school user gets an empty
list, a `404` on detail and an empty photo map (`views/organogram.py:99-118,
218-229`). The org tree itself has no tenant column: it is platform structure, and
`IsVisionStaff` is what keeps school users out.

Payroll FLS is enforced in the serializer, not the view, so it holds on `me`,
`retrieve`, `list` and every write (`serializers.py:772-806`).

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py` | `PlatformStaffProfile`, `OrgNode`, `Position`, `PositionAssignment`, `MatrixReport` and their invariants |
| `services/organogram.py` | Assignment, profile sync, manager chain, tree builder, vacancies, the four climb modes |
| `views/organogram.py` | Profile viewset (incl. `me`, `photos`) and the four organogram viewsets |
| `serializers.py` | Brief vs full profile, payroll FLS with owner exception, node/position/assignment/matrix shapes |
| `management/commands/seed_organogram.py` | Idempotent starter tree, seats, profiles and round-robin seating |
| `vs_workflow/services/approvers.py` | Consumes the climb modes to resolve approvers |
| `core/exceptions.py` | Turns a PROTECT-blocked delete into a `409` naming the blockers |

## 11. Test coverage & gaps

`OrganogramAccessTests` (`test_organogram_access.py:25`) is the access contract: an
ordinary platform employee reading the whole chart without any RBAC key, the
chart-safe profile list, authentication still required, full-name search, the
history-free `current` payload, brief versus full profile, the owner exception,
the HR key unlocking the profile but not payroll, vacancies staying permissioned,
writes still requiring `manage`, a school user refused the tree, and profile
search/detail/photos scoped to the tenant.

Also covered: `OrganogramTreeTests` (`tests.py:410`) for nesting and the
inactive-parent root case; `OrgNodeSerializerUniquenessTests` (`tests.py:449`) for
the three name-uniqueness cases; `OrganogramListQueryTests` (`tests.py:507`) for
the position and node lists not being N+1; `SeedOrganogramCommandTests`
(`tests.py:562`), which **currently errors on Windows** - the command prints a `→`
(U+2192) and a cp1252 stdout cannot encode it, so the run dies with
`UnicodeEncodeError` rather than a logic failure
(`management/commands/seed_organogram.py:195`);
`OrgNodeDeleteProtectionTests` (`tests.py:588`) for the `409`
across every tier, seats blocking a delete, and an empty leaf still deleting; and
`MyPositionAssignmentsTests` (`tests.py:365`) for self-service history and the
`?user=` bypass being refused.

Not covered: the assignment `PATCH` path described in §8 (two current primaries,
inverted dates); writing `position_id` straight onto a profile; the query cost of
`vacancies` and `tree`; matrix-report creation and its self-reference guard; and
`DEPARTMENT_HEAD` resolution walking past a headless node.
