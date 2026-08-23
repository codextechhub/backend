# ticket_visibility_permissions

Who may see a ticket, who may act on it, and who counts as "the support desk".
This slice owns `services/visibility.py`, `permissions.py`, `constants.py`'s
`TicketPermission`, and the seeder. The surfaces those rules protect are
`ticket_lifecycle` and `ticket_conversation_attachments`.

This is the slice to read first if you are changing anything in this module. The
support desk is the only place in the repo where one queryset deliberately spans
every tenant, and the rules that keep that safe are all in one file.

---

## 1. What it is (and what it is NOT)

- **Visibility here is participation, not tenancy.** Being in the same school as
  somebody does not let you read their ticket. The default boundary is
  `requester OR assignee` (`services/visibility.py:103`), and a ticket
  conversation can contain a person's salary query, their health note or their
  complaint about a colleague.
- **`tickets.ticket.view` does not grant school-wide ticket access.** The comment
  at `services/visibility.py:99-102` says so and the code agrees: the key appears
  nowhere in `visible_tickets_qs`. The key that widens the boundary to the whole
  tenant is `tickets.ticket.manage` (104-105).
- **"Support" is an RBAC fact, not a user column.** `is_support_user`
  (`services/visibility.py:14`) is *on the platform tenant* **and** *holds
  `tickets.ticket.manage`*. There is no `user_type` involved: `vs_user`'s persona
  column was dropped (`vs_user/migrations/0009_drop_user_type.py`).
- **The tenant-kind half of that test is load-bearing.** A school user granted
  `tickets.ticket.manage` manages tickets inside their own tenant and must never
  inherit the cross-tenant span. That is asserted directly
  (`tests.py:368-381`).
- **The permission class is not the boundary.** `HasTicketRBACPermission`
  (`permissions.py:11`) decides whether a *call* is allowed; which *rows* come
  back is decided separately by `visible_tickets_qs` / `can_view_ticket`. Most
  actions declare no key at all and rest entirely on the second.
- **There is no branch dimension.** `Ticket.branch` exists and nothing in this
  file reads it. A branch admin sees every branch's tickets.
- **Hidden is `404`, never `403`.** `get_object` raises `NotFound`
  (`views.py:146-148`), so a caller cannot use the error code to learn that a
  ticket exists.

## 2. The nine keys

Declared in `TicketPermission` (`constants.py:70`), seeded by
`manage.py seed_ticket_permissions`.

| Key | Sensitivity | Checked where | Seeded to |
|---|---|---|---|
| `tickets.ticket.view` | NORMAL | **nowhere in this module's API** - only the export dataset and the console overview card | `school_admin`, `branch_admin`, `teacher` |
| `tickets.ticket.update` | NORMAL | `can_update_ticket_fields` (`visibility.py:139`) | `school_admin`, `branch_admin` |
| `tickets.ticket.manage` | SENSITIVE | `is_support_user`, `visible_tickets_qs`, `can_view_ticket`, `can_manage_ticket`; the `transition` action key | `school_admin`, `branch_admin`, platform roles |
| `tickets.ticket.assign` | SENSITIVE | `can_assign_ticket`; the `assign` and `eligible-assignees` action keys | platform roles only |
| `tickets.comment.post` | NORMAL | `can_comment_on_ticket` | `school_admin`, `branch_admin`, `teacher` |
| `tickets.internal_note.post` | SENSITIVE | `can_add_internal_note` - and therefore also *reading* notes | platform roles only |
| `tickets.attachment.create` | NORMAL | `can_attach_to_ticket` | `school_admin`, `branch_admin`, `teacher` |
| `tickets.audit.view` | SENSITIVE | the `audit` action key | platform roles only |
| `tickets.report.view` | NORMAL | **nowhere** | `school_admin`, `branch_admin` |

Seeding (`management/commands/seed_ticket_permissions.py`):

- `xvs_super_admin` and `xvs_platform_admin` get all nine on the `codex` platform
  tenant (108-131). If that tenant is missing the command warns and skips - it
  does not fail.
- `SCHOOL_DEFAULT_KEYS` (view, comment, attachment) go to all three school
  prebuilts; `SCHOOL_ADMIN_EXTRA_KEYS` (update, manage, report) additionally to
  `school_admin` and `branch_admin` (18-27, 133-149).
- Existing tenant role templates are backfilled by matching their key against
  `^(school_admin|branch_admin|teacher)(-\d+)?$` (151-177), the `-<branch>`
  suffix being how a branch-scoped copy of a prebuilt is named.

**Creating a ticket has no key on purpose** (`constants.py:70-72`). Filing is the
one escalation route that must not depend on a grant somebody forgot to give
you - a consultant whose role holds nothing but `view` keys can still open a
ticket, and that is pinned by `test_consultant_role_can_create_a_ticket`
(`tests.py:465-494`).

## 3. The two gates

### The call gate - `HasTicketRBACPermission` (`permissions.py:11`)

```python
TICKET_PERMISSIONS = [IsAuthenticatedAndActive & HasTicketRBACPermission]
```

In order:

1. `IsAuthenticatedAndActive` - authenticated, not SUSPENDED/LOCKED/DEACTIVATED,
   and `TenantSurfaceAllowed` (`vs_rbac/permissions.py:213-229`), which refuses a
   PENDING tenant anything outside `pending_tenant_surface`.
2. `is_support_user(request.user)` → allowed, whatever key the action names.
3. Otherwise `HasRBACPermission`, which reads `view.rbac_permission` - here a
   property returning `RBAC_ACTION_KEYS.get(self.action)` (`views.py:62-71`) -
   and **passes when there is no key** (`vs_rbac/permissions.py:320-347`).

The support bypass sits *after* `IsAuthenticatedAndActive`, so the pending-tenant
gate still runs for CX staff. It is applied by ANDing, not by the bypass
returning early inside `HasRBACPermission`, which is what stops that gate being
skipped.

`is_vision_super_admin` bypasses `HasRBACPermission` entirely
(`vs_rbac/permissions.py:301-302`), one layer further in.

### The row gate - `visible_tickets_qs` (`services/visibility.py:82`)

```python
qs = Ticket.all_objects.select_related("requester__tenant", "assignee__tenant",
                                       "tenant", "branch")
if not authenticated:            return qs.none()
if is_support_user(user):        return qs                  # every tenant
qs = qs.filter(tenant=user.tenant)
visibility = Q(requester=user) | Q(assignee=user)
if has_ticket_permission(user, MANAGE, tenant=user.tenant):
    visibility |= Q(tenant=user.tenant)                     # the whole school
return qs.filter(visibility)
```

`all_objects`, not `objects`: the ambient `TenantAwareManager` would pin every
query to the asserted tenant and destroy the support span. The scoping is
therefore explicit on every line, which is the right way round - the one place
that must cross tenants does so visibly.

The `select_related` chain is not decoration: `TicketUserSerializer` reads
`tenant.kind` for both the requester and the assignee, so without the two
`__tenant` joins every row in the list costs two extra queries.

`can_view_ticket` (110) is the same rule for one row, and the two must stay in
step - `get_object` fetches by pk and asks `can_view_ticket`, so a divergence
would show up as a row that lists but will not open, or the reverse.

## 4. Who can do what

| Question | Function | Answer |
|---|---|---|
| See it | `can_view_ticket` (110) | support; else same tenant **and** (requester, assignee, or holds `manage`) |
| Edit title/description/category/priority | `can_update_ticket_fields` (134) | anyone who can manage it, the requester, or a holder of `update` |
| Assign it | `can_assign_ticket` (143) | support, or a holder of `assign` in the ticket's tenant |
| Change status | `can_manage_ticket` (124) | support, the assignee, or a holder of `manage` |
| Reply publicly | `can_comment_on_ticket` (150) | must be able to see it; then support, requester, assignee, or a holder of `comment.post` |
| Attach a file | `can_attach_to_ticket` (160) | as for replying, with `attachment.create` |
| Write or read an internal note | `can_add_internal_note` (169) | support, the assignee, or a holder of `internal_note.post` |

Three details worth knowing before you change any of them:

- **The requester can edit their own ticket's text** (137-138) but cannot change
  its status - `test_requester_cannot_transition_own_ticket` (`tests.py:457-463`).
- **The assignee can progress a ticket without any broader grant** (127-129).
  That is what lets a CX agent work a ticket in a tenant where they hold nothing.
- **`can_manage_ticket` and `can_assign_ticket` do not check visibility first.**
  They are safe only because every caller reaches them through `get_object()`,
  which has already refused an invisible ticket. A new service caller that skips
  that step would skip the boundary.

**Assignees must be support-capable.** `assign_ticket` refuses anybody
`is_support_user` rejects (`services/tickets.py:97-101`), so a customer can never
become a ticket owner. The picker behind it is `eligible_support_users_qs` (§5).

## 5. Three answers to "who works the support desk"

This module asks that question in three places and gets three different answers.
The differences are the module's most consequential defect
(`ticket_code_issues.md` §2) and are summarised here because you cannot read
this file safely without knowing about them.

| Asked by | Implementation | Honours groups | Honours role status | Honours role denies | Honours personal overrides | Honours `is_active` |
|---|---|---|---|---|---|---|
| The gate - `is_support_user` (`visibility.py:14`) | `user_has_rbac_permission` → the evaluator | yes | yes | yes | yes | n/a |
| The picker - `eligible_support_users_qs` (`visibility.py:27`) | hand-built `Exists` subqueries | yes | yes | yes | **no** | yes |
| The notification queue - `support_recipients` (`services/notifications.py:32`) | one `filter()` on direct role permissions | **no** | **no** | **no** | **no** | **no** |

There is a canonical helper for exactly this,
`vs_rbac.evaluator.resolve_users_with_permission` (`vs_rbac/evaluator.py:244`),
which handles every column above and whose docstring states the rule the desk
needs: *somebody this function nominates cannot be somebody `has_permission`
would then refuse*. Neither of the two hand-built variants uses it.

The picker's own comment (`visibility.py:37-42`) explains why it was widened to
`ANY_BRANCH` - so that somebody the gate admits appears in the assignment list.
That is the same instinct applied to one column and not the rest.

## 6. What the boundary writes

Nothing. Every function in `services/visibility.py` is a read. The permission
class writes nothing either. Refusals are not audited: a `404` on a hidden
ticket, a `403` on an assignment attempt and a `TenantNotLive` all leave no trace
in `TicketAuditLog` or in `vs_audit`.

The one thing worth knowing about cost: `has_permission` caches its result on the
user object for the request (`vs_rbac/evaluator.py:204-221`), so the repeated
`is_support_user` calls across the permission class, the queryset, `get_object`
and `capabilities` collapse to one evaluation per key per request.

## 7. Worked example

Bright Star has two branches, Ikeja and Yaba. Ngozi (Bursar, Ikeja) raises a
ticket about a printing bug. Four people ask for it:

| Caller | Holds | `GET /tickets/4471/` |
|---|---|---|
| Ngozi, the requester | `view`, `comment.post`, `attachment.create` (teacher defaults) | `200` - she is the requester |
| Tunde, another Ikeja bursar | the same three keys | `404` - the `view` key is not school-wide access |
| Bola, Bright Star's school admin | + `update`, `manage`, `report.view` | `200` - `manage` widens the boundary to the whole tenant |
| Kemi, Yaba's **branch** admin | the same as Bola | `200` - and that is the bug: branch never narrows anything (`ticket_code_issues.md` §6) |
| Ada, CX support on the platform tenant | `manage` on `codex` | `200`, asserting `?tenant=codex` |
| Chidi, admin at a different school | `manage` on his own tenant | `404` - the tenant-kind test in `is_support_user` |

Ada then adds an internal note. Ngozi's next read of the thread returns one
comment, her own, with nothing to indicate a second exists - and her ticket's
`comments_count` says `1`.

## 8. Gotchas / known limitations

Full evidence in **`error/tickets/ticket_code_issues.md`**.

- **The support desk has three definitions and they disagree** (§5 above,
  `ticket_code_issues.md` §2). A person locked out by a personal DENY still
  appears in the assignment picker and is still emailed every new ticket.
- **The export bypasses the participant boundary.** The `support.tickets`
  dataset is gated on `tickets.ticket.view` - a key seeded to every teacher -
  and its base queryset is every ticket in the tenant
  (`export_datasets.py:30-32,42-45`). What the API refuses a teacher row by row,
  the Export Centre hands them as a spreadsheet
  (`ticket_code_issues.md` §3).
- **Branch is never a scope.** No filter, no narrowing, no column
  (`ticket_code_issues.md` §6).
- **`tickets.ticket.view` and `tickets.report.view` are checked nowhere in this
  module** (`ticket_code_issues.md` §12). A reviewer reading the seeder will
  reasonably assume both do something here.
- **Reading internal notes and writing them are the same key**
  (`visibility.py:176-177`), so there is no read-only internal-note role - a
  deliberate simplification, but one that surprises when someone asks for a
  reviewer who can see the desk's notes without adding to them.
- **`sees_internal_notes_by_default` asks about `user.tenant`, not the ticket's**
  (`visibility.py:181-184`). Correct today - the only people who see internal
  notes hold the key on their own tenant - but it means the list counts and the
  detail payload answer the question two different ways.
- **`can_manage_ticket` / `can_assign_ticket` presume `get_object` ran first**
  (§4). Nothing in the functions enforces it.
- **Refusals are not audited.** Nothing records that somebody tried to open a
  ticket they could not see.
- **Justified by design:** ticket creation is keyless (`constants.py:70-72`).
- **Justified by design:** the support span is unconditional for a platform
  `manage` holder - CX cannot work a queue they cannot see, and the tenant-kind
  test is what keeps that from reaching a school.

## 9. Permissions & tenant isolation

Isolation in this module has exactly three shapes, and all three are visible in
`visible_tickets_qs`:

1. **Ownership** - `requester` or `assignee`. The default, and the only one a
   caller with no ticket keys has.
2. **Tenancy** - `tenant = user.tenant`, applied before any grant is consulted,
   and widened to the whole tenant only by `manage`.
3. **The support span** - unfiltered, for a platform-tenant holder of `manage`.

There is no fourth. In particular, `?tenant=` cannot be used to widen anything:
no view in the module sets `platform_cross_tenant_param`, so asserting another
tenant's slug is a `404` from `TenantJWTAuthentication`
(`vs_rbac/authentication.py:119-127`) before any of this runs. A CX agent works
every school's tickets while asserting `codex`.

Impersonation rides the same rules with the impersonated user's authority:
`request.user` is the effective user, so a CX agent wearing a school admin's
identity sees exactly that admin's tickets, and the audit records both
(`services/audit.py:15-17`).

## 10. Code map

| File | Responsibility |
|---|---|
| `services/visibility.py:14-23` | `is_support_user` - the platform-tenant + `manage` test |
| `services/visibility.py:27-73` | `eligible_support_users_qs` - the assignment picker |
| `services/visibility.py:82-106` | `visible_tickets_qs` - the row gate |
| `services/visibility.py:110-184` | The per-action predicates |
| `permissions.py:11-28` | `HasTicketRBACPermission`, `TICKET_PERMISSIONS` |
| `constants.py:70-82` | `TicketPermission` - the nine keys |
| `management/commands/seed_ticket_permissions.py` | Registration, platform grants, school defaults, backfill |
| `views.py:57-71` | `pending_tenant_surface`, `RBAC_ACTION_KEYS`, the `rbac_permission` property |
| `views.py:139-149` | `get_object` - where hidden becomes `404` |
| `vs_rbac/permissions.py:204-347` | `IsAuthenticatedAndActive`, `TenantSurfaceAllowed`, `HasRBACPermission` |
| `vs_rbac/evaluator.py:244-306` | `resolve_users_with_permission` - the helper this module does not use |

## 11. Test coverage & gaps

- `test_visibility_is_participant_manager_and_support_scoped`
  (`tests.py:355-366`) - the three shapes, in one test.
- `test_school_manage_grant_does_not_leak_cross_tenant` (`tests.py:368-381`) -
  the tenant-kind half of `is_support_user`, asserted three ways.
- `test_school_wide_visibility_is_not_granted_by_view_permission`
  (`tests.py:383-394`) - both a user with no grants and a peer with the ordinary
  three.
- `test_cross_tenant_retrieve_is_hidden_as_404`,
  `test_same_tenant_peer_cannot_list_or_open_another_users_ticket`
  (`tests.py:438-455`).
- `test_requester_cannot_transition_own_ticket`,
  `test_requester_cannot_assign_ticket`, `test_requester_cannot_view_audit_trail`.
- `test_school_manager_with_grant_can_transition_via_api`
  (`tests.py:526-533`) - the positive side of the same boundary.
- `test_assignment_options_include_only_active_ticket_handlers`
  (`tests.py:504-524`) - the picker excludes a platform user without the key, and
  assigning them anyway is a `400`.
- `test_consultant_role_can_create_a_ticket` (`tests.py:465-494`) - keyless
  creation, with the role asserted to hold nothing but `view` actions.
- `TicketPermissionSeedTests` (`tests.py:706-723`) - the command registers the
  keys and attaches the school defaults.
- `vs_rbac/tests/test_tenant_isolation.py` includes ticket models in its sweep.

What the suite does not cover:

1. **The three definitions of "support" agreeing.** No test grants a CX user the
   ticket keys through a permission *group*, or denies one through a personal
   override, and then compares the gate, the picker and the notification queue.
   That is the test that would turn §5 red.
2. **`tickets.ticket.assign` held by a school user.** Nothing asserts what a
   school admin with `assign` can see through `eligible-assignees` - today, the
   platform's whole support roster with names and email addresses.
3. **The export boundary.** No test asserts that a teacher holding
   `tickets.ticket.view` cannot export tickets they cannot open.
4. **Branch.** No test has a multi-branch school and asserts anything at all
   about which branch's tickets a branch admin sees.
5. **`tickets.ticket.update` as the only route in** - the `update` key's branch
   of `can_update_ticket_fields` is never exercised.
6. **The pending-tenant gate**, in either direction.
7. **`is_vision_super_admin`'s bypass** through this module.
