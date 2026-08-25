# workflow_approvers

Who may decide a stage. The four approver sources, the reusable approver groups,
the per-tenant override that repoints one step of a shared template, delegation,
and the two rules applied to every source without exception: tenant containment
and no self-approval. The engine that calls this is `workflow_engine_routing`;
the votes it enables are `workflow_actions_lifecycle`.

Routes: `approver-groups/`, `stage-approvers/`, `delegations/`.

---

## 1. What it is (and what it is NOT)

- **Resolution happens once, at activation, and is then frozen.**
  `resolve_approvers` builds the list; `_activate_stage` writes it into
  `WorkflowStageApprover` rows; every later check reads those rows. A role change
  mid-review never invalidates somebody already notified and mid-decision.
- **The freeze is also the trap.** A stage that activates with zero eligible
  approvers is permanently unreachable for that attempt, however the roles change
  afterwards. That is what `workflow_parking_release` exists to repair.
- **A stage names authority by role *key*, not by foreign key.** The key is what
  resolution reads, because a shared template is published once with no tenant and
  must name the same authority in every tenant that runs it
  (`services/approvers.py:78-97`). The FK is an anchor for tenant templates only.
- **Approver groups are tenant-local, always.** `WorkflowApproverGroup.tenant` is
  non-null (`models.py:141-144`): approval authority is never global.
- **Group membership is heterogeneous and mostly live.** `USER` rows are static;
  `ROLE` and `POSITION` rows are resolved at every activation, so staff changes
  flow through without a group edit (`models.py:189-198`).
- **An override changes who approves, and nothing else.** Advance rule, rejection
  policy and routing stay with the template (`models.py:355-358`), so a tenant
  cannot use an override to soften a shared control.
- **Delegation adds people, it does not move authority.** A non-exclusive
  delegation leaves the delegator eligible as well - which is what deadlocks a
  UNANIMOUS stage (§8).
- **Nobody can approve their own submission**, on any source, ever
  (`services/approvers.py:432-434`).

## 2. The four sources

`ApproverSource` (`constants.py:77`). A stage picks exactly one.

| Source | Resolves to | Config on the stage |
|---|---|---|
| `ROLE` (default) | Active assignees of a role key, inside the requesting tenant | `approver_role_key`, `approver_scope` |
| `WORKFLOW_GROUP` | The live membership of a named group | `approver_group` |
| `DYNAMIC_ROLE` | The role named by the first matching rule | `dynamic_rules` |
| `ORGANOGRAM` | Holders of a seat reached by climbing from the requester | `organogram_target`, `organogram_levels`, `organogram_position` |

A fifth, `RBAC_PERMISSION`, was removed. The reason is recorded in the enum's own
docstring (`constants.py:89-93`): permission keys are a developer vocabulary
template builders cannot be expected to know, and every key resolved through
roles anyway.

**An unrecognised source raises** rather than resolving to nobody
(`services/approvers.py:410-418`). That is deliberate and load-bearing: a
skip-enabled stage would act on an empty list by skipping itself, so a source the
engine has not been taught must fail loudly instead of waving a document through.

### `approver_scope`

`BRANCH` narrows role lookups to branch-limited assignments for the instance's
branch plus tenant-wide ones; `SCHOOL` and `PLATFORM` count tenant-wide
assignments only (`services/approvers.py:118-119`). The branch condition itself
comes from `vs_rbac._assignment_branch_q` rather than being spelled out here -
it was a fourth copy of one rule, and a copy is free to drift from the permission
gate, which would mean nominating an approver `has_permission` then refuses.

### `ORGANOGRAM`

Four climb modes (`constants.py:113-118`): the requester's direct manager, N
levels up, their department head, or the holders of a named seat. All four go
through `OrganogramService`, degrade to an empty list if `vs_user` is
unavailable, and exclude the requester inside the service helpers.

The seats are **platform-global**, which is why containment is applied once for
every source rather than per branch - see §4.

## 3. Endpoint map

| Method + path | Key | Notes |
|---|---|---|
| `GET approver-groups/` | `workflow.group.view` **or** `workflow.template.manage` | `?is_active=`, `?search=` |
| `POST/PUT/PATCH/DELETE approver-groups/` | `workflow.group.manage` | Delete refuses while a stage points at it |
| `GET approver-groups/<id>/resolve/?branch=<id>` | view/manage | Per member and in total |
| `POST approver-groups/<id>/members/` | `workflow.group.manage` | `{kind, user|role_key|position_code}` |
| `DELETE approver-groups/<id>/members/<member_id>/` | `workflow.group.manage` | Scoped to this group |
| `GET/POST/PUT/DELETE stage-approvers/` | `workflow.template.view` / `.manage` | The per-tenant override |
| `GET/POST/PUT/DELETE delegations/` | **none** - `IsAuthenticatedAndActive` only | Own rows unless you hold `workflow.template.manage` |
| `POST delegations/<id>/revoke/` | ownership or `workflow.template.manage` | Timestamped, not deleted |

Reading groups deliberately travels with template management as well as the
group's own view key (`views.py:710-719`): a `WORKFLOW_GROUP` stage names a
group, and the template builder cannot offer one it is not allowed to read.
Writing a group still takes the group key.

**Delegations are the one surface in the module with no RBAC key at all.** Any
authenticated, active user in the tenant can create one, and a holder of
`workflow.template.manage` sees everybody's (`views.py:882-888`).

## 4. Resolution, in order (`services/approvers.py:359`)

```text
1. tenant override for this stage?          → override's role or group
   else by stage.approver_source:
      ROLE           → holders of approver_role_key
      WORKFLOW_GROUP → resolve_group_users(stage.approver_group)
      DYNAMIC_ROLE   → holders of the first matching rule's role_key
      ORGANOGRAM     → the climb from the requester
      anything else  → raise UnknownApproverSourceError

2. _tenant_members(base_users, instance.tenant_id)   ← containment, door 1
3. drop the requester
4. active, unrevoked, matching-document-type delegations from the survivors
5. _tenant_members(delegates, instance.tenant_id)    ← containment, door 2
6. drop delegators of *exclusive* delegations
7. one EligibleApprover per (user, on_behalf_of) pair
```

Three properties of that order are deliberate and worth not undoing:

- **Containment is applied once per door, not once per source.** The docstring is
  explicit that ORGANOGRAM is the reason: organogram seats are platform-global,
  so a `SPECIFIC_POSITION` climb - which does not depend on the requester at all -
  could otherwise hand a tenant's document to somebody outside that tenant. The
  role, group and override paths already resolve inside the tenant, so the filter
  is a no-op for them and the one missing guard for the fourth.
- **Delegates go through the same filter** (step 5). `tenant=instance.tenant` on
  the delegation query scopes the *row* and reads like containment without being
  it: nothing constrained the user that row names. Rows written before this rule
  existed are neutralised at resolution rather than deleted, because ignoring a
  row is reversible and destroying somebody's delegation history is not.
- **Step 5 runs before step 6** (`services/approvers.py:467-472`). An exclusive
  delegation naming an outsider must not strip the delegator while contributing
  nobody: a row that is not allowed to add an approver is not allowed to remove
  one either.

`_tenant_members` (`services/approvers.py:123-139`) is the single definition of
"this person may hold approval authority here": not None, `is_active`, and
`tenant_id == instance.tenant_id`, de-duplicated by pk.

## 5. Groups and overrides

### `WorkflowApproverGroup` (`models.py:127`) and its members (`models.py:189`)

Unique on `(tenant, code)`. Members carry a `kind` and exactly one target,
enforced by a database `CheckConstraint` plus three conditional unique
constraints (`models.py:220-243`). `role` and `position` are `PROTECT`: a target
a group points at must not vanish silently.

`resolve_group_users` (`services/approvers.py:143-170`) unions the three kinds
and runs containment. **A deactivated group resolves to nobody** rather than
raising, leaving `skip_if_no_approvers` to decide.

`describe_group_members` (174-212) is the same resolution, row by row, and is
what the Workflow Approver screen renders - so the preview cannot disagree with
an activation.

Deleting a group a live stage points at is refused with `409
APPROVER_GROUP_IN_USE` and the list of stages (`views.py:738-756`), because the
FK is `PROTECT` and the alternative is a 500.

### `WorkflowStageApproverOverride` (`models.py:345`)

One row per `(tenant, stage)`. Only two sources are choosable - `ROLE` or
`WORKFLOW_GROUP` - and a `CheckConstraint` enforces that the chosen one carries
its own target and the other is empty (`models.py:392-403`). Organogram and
dynamic rules stay template-owned.

`stage_override_for` (`services/approvers.py:333-347`) reads through
`all_objects` with an explicit tenant filter, and returns `None` for a tenant-less
context.

## 6. Delegation (`models.py:708`)

A date-ranged grant from one user to another, optionally limited to one
`document_type`, optionally `exclusive`. Revocation is a timestamp, never a
delete.

At resolution (`services/approvers.py:438-447`) a delegation applies when it is
in window, unrevoked, its `document_type` is blank or matches, its delegator is
among the surviving base approvers, and its delegate is not the requester.

A delegate acting for two delegators appears twice, once per delegator, because
`on_behalf_of` differs and the audit trail should show both names.

The write path is guarded in the serializer's context rather than the view: the
tenant is passed in (`views.py:876-880`) so the delegate is looked up **inside
the tenant**, which is how a delegation naming somebody in another tenant is
refused at creation as well as ignored at resolution.

## 7. Worked example

Bright Star's purchase-order ladder has one stage, `po-approval`, source `ROLE`,
key `po-approver`, scope `SCHOOL`, rule `UNANIMOUS`.

Two people hold `po-approver`: Adaeze and Chidi. Adaeze is on leave and has
delegated to Femi, **not exclusively**.

Tunde submits a PO. At activation:

```text
base (role holders)      → Adaeze, Chidi
containment              → Adaeze, Chidi          (both Bright Star)
drop the requester       → Adaeze, Chidi          (Tunde is neither)
delegations from base    → Adaeze → Femi (non-exclusive)
containment on delegates → Femi                   (Bright Star)
exclusive delegators     → {}                     (not exclusive)

snapshot: [Adaeze (-), Chidi (-), Femi (on behalf of Adaeze)]
eligible_count = 3
```

Chidi approves. Femi approves. The stage does **not** advance: `UNANIMOUS`
requires `approved_count >= eligible_count`, which is 2 of 3, and the third row
is Adaeze - who is on leave, which is why she delegated. The PO waits until she
comes back (`workflow_code_issues.md` §5).

Had the delegation been exclusive, Adaeze would have been dropped in step 6, the
snapshot would be two rows, and Chidi plus Femi would have completed it.

And a preview of the same stage, before anybody submits:

```text
POST /v1/workflow/templates/preview-approvers/?tenant=bright-star
{"approver_source": "ROLE", "approver_role_key": "po-approver",
 "approver_scope": "SCHOOL", "requester": 412}
  → {"count": 3, "approvers": [{"user": {…Adaeze}, "on_behalf_of": null},
                                {"user": {…Chidi},  "on_behalf_of": null},
                                {"user": {…Femi},   "on_behalf_of": {…Adaeze}}]}
```

the same resolver, so the builder sees exactly what the engine will freeze.

## 8. Gotchas / known limitations

Full evidence in **`error/workflow/workflow_code_issues.md`**.

- **A non-exclusive delegation deadlocks a UNANIMOUS stage.** The delegate is
  added without the delegator being removed, and unanimity counts snapshot rows
  (`workflow_code_issues.md` §5).
- **`resolve_approvers` runs twice per activation** - once inside
  `_activate_stage` and once in the caller that decides the skip
  (`workflow_code_issues.md` §9).
- **Delegations have no permission key and no bound on their window.** Any
  authenticated user can create one running for a decade, and nothing checks that
  they hold any approval authority to delegate
  (`workflow_code_issues.md` §16).
- **`preview-approvers` takes an arbitrary requester id** and resolves in that
  user's tenant (`views.py:230-234`). Gated on `workflow.template.view`, which is
  platform-only today; a school role holding it would gain a cross-tenant people
  lookup (`workflow_code_issues.md` §16).
- **A group with `is_active = False` resolves to nobody silently.** Combined with
  `skip_if_no_approvers = True` (no longer the default, but still settable), deactivating a group makes every stage that
  names it auto-approve. There is no warning at deactivation time and no
  coverage check equivalent to `workflow_role_coverage` for groups.
- **`role_holder_ids` and `_users_for_role_key` do not honour personal
  permission overrides.** They read role assignments, which is the right question
  for "who holds this role"; a reader expecting parity with
  `vs_rbac.resolve_users_with_permission` should know the two answer different
  questions.
- **Justified by design:** an unknown approver source raises rather than
  resolving to nobody.
- **Justified by design:** containment is applied at the two doors rather than
  per source, and delegate containment runs before exclusivity is applied.
- **Justified by design:** the role *key* is the lookup and the FK is only an
  anchor, so one shared template names the same authority everywhere.

## 9. Permissions & tenant isolation

| Surface | Gate |
|---|---|
| Approver groups: read | `workflow.group.view` or `workflow.template.manage` |
| Approver groups: write | `workflow.group.manage` (`SENSITIVE`) |
| Stage overrides: read / write | `workflow.template.view` / `.manage` |
| Delegations | authentication only; your own rows unless you hold `workflow.template.manage` |

Every queryset here filters `tenant=self.get_tenant()` explicitly through
`all_objects` (`views.py:724-734`, `856-863`, `882-888`), and the member and
override write paths resolve their targets inside `context["tenant"]`, so a
role, user, position or group from another tenant is refused at validation
rather than at resolution.

The containment rules in §4 are the last line: even a row that somehow named an
outsider contributes nobody, and cannot strip anybody either.

## 10. Code map

| File | Responsibility |
|---|---|
| `services/approvers.py:41-97` | `_users_for_roles`, `_users_for_role_key` |
| `services/approvers.py:101-119` | `stage_role_key`, `_role_base_users` |
| `services/approvers.py:123-139` | `_tenant_members` - the containment rule |
| `services/approvers.py:143-218` | `resolve_group_users`, `describe_group_members` |
| `services/approvers.py:222-264` | `match_dynamic_rule`, `_dynamic_role_base_users` |
| `services/approvers.py:279-292` | `role_holder_ids` - the public read-only entry point |
| `services/approvers.py:296-329` | `_organogram_base_users` |
| `services/approvers.py:333-355` | `stage_override_for`, `_override_base_users` |
| `services/approvers.py:359-496` | `resolve_approvers` |
| `services/roles.py` | `ensure_approver_role` - provisioning creates the role, grants nobody |
| `management/commands/workflow_role_coverage.py` | The gap report |
| `views.py:700-829` | `WorkflowApproverGroupViewSet` |
| `views.py:832-868` | `WorkflowStageApproverOverrideViewSet` |
| `views.py:871-910` | `ApprovalDelegationViewSet` |
| `models.py:127-243` | Group and member, with their four constraints |
| `models.py:345-410` | The override and its check constraint |
| `models.py:708-762` | `ApprovalDelegation` |

## 11. Test coverage & gaps

This is the most heavily tested area of the module.

- `RoleSourceResolveApproversTests` (`tests/test_services.py:316-478`) - active
  assignee eligible; requester excluded even when assigned; revoked assignment,
  inactive user and archived role all resolve empty; the key resolves in the
  requesting tenant; another tenant's assignment is not eligible; SCHOOL scope
  ignores branch-limited assignments; delegation expands the list.
- `GroupSourceResolveApproversTests` (`569-733`) - each member kind, a vacant
  position, mixed membership unioned and de-duped, requester excluded, inactive
  and empty groups, a position holder outside the tenant excluded, branch scope
  narrowing role members only, delegation, and `describe_group_members`.
- `DynamicRoleResolveTests` (`818-953`) - first match wins, fallback, boundary
  value, no match and no fallback, a missing field, compound conditions,
  requester excluded, delegation, and the "why" trace.
- `StageApproverOverrideTests` (`1072-1186`) - override wins over the central
  role, to a group, scoped to its own tenant, still excludes the requester, still
  expands delegation, one per tenant per stage, and the source/target constraint.
- `OrganogramSourceResolutionTests` (`1188-1575`) - live resolution never
  memoised, vacant seats, the requester never their own approver, an unstaffed
  climb parks rather than auto-approving, a climb reaching another tenant's user
  resolves to nobody, `SPECIFIC_POSITION` cannot cross the boundary, and a
  platform-tenant climb is not emptied by containment.
- `DelegationTenantContainmentTests` (`1577-1760`) - a foreign delegate is not
  eligible, an exclusive cross-tenant delegation does not strip the delegator, a
  deactivated delegate, and both same-tenant cases.
- `ApproverGroupApiTests` (`tests/test_approver_groups_api.py:69-347`) - 30 tests
  over permissions, cross-tenant refusals, member management and `resolve`.
- `StageApproverOverrideApiTests` (`439-551`) and `DelegationWriteBoundaryTests`
  (`tests/test_tenant_scoping.py:230-306`).

What it does not cover:

1. **The delegation/UNANIMOUS interaction** in §8's first item. Delegation is
   tested against `ANY`-style resolution and never against a unanimity threshold.
2. **Delegation as an endpoint** - create and revoke are exercised for the tenant
   boundary; the `revoke` permission branch, the window, and `document_type`
   filtering are not.
3. **A group deactivated under a live stage**, and the auto-approval it causes
   when `skip_if_no_approvers` is true.
4. **`role_holder_ids`** as a public helper.
5. **Two delegations to the same delegate from different delegators**, which the
   code explicitly supports.
