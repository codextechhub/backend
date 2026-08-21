# workflow_templates

The blueprint: what a `WorkflowTemplate` holds, how stages and routes are
published, and the two-tier model that lets the platform publish one approval
path every tenant runs until it adjusts its own. The engine that walks a
published template is `workflow_engine_routing`; who approves each stage is
`workflow_approvers`.

Routes are mounted at `/v1/workflow/` (`apps/urls.py:32`), from
`vs_workflow/urls.py`. This slice owns `templates/`.

---

## 1. What it is (and what it is NOT)

- **A template is a definition, not a version.** Publishing the same
  `(tenant, branch, document_type, code)` updates the row in place
  (`services/templates.py:187-205`). There is no version history, no draft, and
  no publish-time snapshot: an instance holds a `PROTECT` foreign key to the
  template and reads its stages live.
- **Stages are never deleted, only retired.** A publish that omits a stage code
  stamps `retired_at` on it (`services/templates.py:256-260`), because running
  instances hold `PROTECT` references to it. Routes and dynamic rules carry no
  instance references and are therefore replaced wholesale (262-273, 249).
- **A tenant-less template is the shared one.** `WorkflowTemplate.tenant` is
  nullable, and null means "every tenant runs this until it publishes its own"
  (`models.py:52-56`). Only a caller on the platform tenant may publish one
  (`views.py:400-414`).
- **A tenant's own version is switched off, never deleted.**
  `use-platform-version` sets `is_active = False` (`views.py:453-455`), which
  drops it out of the submission cascade so the next request falls through to
  the platform template. Deleting is not an option: instances `PROTECT` the
  template they ran under, so the copy that has actually been used is exactly the
  one that cannot be removed.
- **Branch narrows, it does not replace.** A branch-scoped template takes
  precedence for documents from that branch; tenant-wide and platform templates
  remain listable and remain in the cascade (`views.py:209-213`).
- **The template API is read plus publish. There is no `PUT`, `PATCH` or
  `DELETE`.** `WorkflowTemplateViewSet` is a `GenericViewSet` with only
  `ListModelMixin` and `RetrieveModelMixin` (`views.py:145-147`); every write
  goes through `POST templates/publish/`.
- **It is not a state machine definition in the BPMN sense.** There are two
  stage kinds, an ordered route graph with JSON conditions, and nothing else -
  no parallel gateways, no timers, no sub-processes, no escalation ladder.

## 2. Domain model

Five models, all in `models.py`.

### `WorkflowTemplate` (`models.py:45`)

| Field | Notes |
|---|---|
| `id` | 8-character shortuuid, not an integer |
| `tenant` | `PROTECT`, nullable. Null = the shared platform template |
| `branch` | `PROTECT`, nullable. Set when a branch admin publishes for their site |
| `document_type` | Dotted token, e.g. `finance.journal`, `payments.payout_batch` |
| `code` | Slug naming the variant, e.g. `standard`, `high_value` |
| `notification_events` | `JSONField` of `{event_key: bool}` - see `workflow_notifications_audit` §3 |
| `is_active` | The switch behind `use-platform-version` |
| `objects` / `all_objects` | `TenantAwareManager(include_global=True)` / plain |

Unique on `(tenant, branch, document_type, code)`. Two indexes, the second
deliberately ordered for the cascade: `(document_type, code, tenant, is_active)`.

`include_global=True` on the manager is what makes the ambient-tenant read also
return tenant-null rows, which is how the shared template stays visible to
everybody.

### `WorkflowStage` (`models.py:246`)

The node. Carries its own `code` (unique per template), `label`, `kind`
(`APPROVAL` or `BRANCH`), `order`, the whole approver configuration
(`workflow_approvers` §2), and four routing-relevant settings:

| Field | Effect |
|---|---|
| `advance_rule` | `UNANIMOUS` (default), `QUORUM`, `ANY` |
| `quorum_count` | Threshold when the rule is `QUORUM` |
| `on_rejection` | `TERMINAL` (default) or `RETURN_TO_REQUESTER` |
| `skip_if_no_approvers` | Default `True`. Every money ladder sets it `False` |
| `inclusion_condition` | JSON condition; the stage is skipped when it is false |
| `retired_at` | Soft-retirement stamp; `is_retired` reads it |

### `WorkflowRoutePath` (`models.py:464`)

A directed edge `from_stage → to_stage`, both nullable: a null `from_stage` is
the entry edge, a null `to_stage` means the workflow ends APPROVED there.
Evaluated in ascending `order`, first match wins, a null condition always
matches. **There is no unique constraint on `(from_stage, order)`**, so two
edges can share a position and the tie is broken by insertion order.

### `WorkflowStageDynamicRule` (`models.py:413`)

Ordered "when this, then that role" rules for a `DYNAMIC_ROLE` stage. Unique on
`(stage, order)`. A rule with a null condition is the fallback and publishing
enforces that it is last (`services/templates.py:145-149`).

### `WorkflowStageApproverOverride` (`models.py:345`)

One tenant's own approver for a stage it did not author. Covered in
`workflow_approvers` §5; it lives beside the template because it is the other
way a tenant adjusts a shared definition without cloning it.

## 3. Endpoint map

`?tenant=<slug>` is required on every route: no view sets
`tenant_param_required = False`. No view sets `platform_cross_tenant_param`
either, so a platform actor reading a tenant's version does it through
`compare`, not by asserting the tenant's slug.

| Method + path | Key | Response |
|---|---|---|
| `GET templates/` | `workflow.template.view` | Paginated `WorkflowTemplateReadSerializer` |
| `GET templates/<id>/` | `workflow.template.view` | `WorkflowTemplateReadSerializer`, **unwrapped** (§8) |
| `POST templates/publish/` | `workflow.template.manage` | `201` + the published template |
| `POST templates/<id>/use-platform-version/` | `workflow.template.manage` | The platform template |
| `GET templates/<id>/adoption/` | `workflow.template.manage` + platform tenant | Adoption counts |
| `GET templates/<id>/compare/?with=<id>` | `workflow.template.manage` + platform tenant | Structural diff |
| `POST templates/preview-approvers/` | `workflow.template.view` | Who would approve, without saving |

**Both keys are seeded to platform roles only**
(`management/commands/seed_workflow_permissions.py:50-51,120-129`), and nothing
anywhere else in the repo grants them. Out of the box no school user can list,
read or publish a template.

### The queryset (`views.py:204-219`)

```python
qs = WorkflowTemplate.all_objects.filter(
    Q(tenant=self.get_tenant()) | Q(tenant__isnull=True))
if branch is not None:
    qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
return qs.prefetch_related("stages", "routes").order_by("document_type", "code")
```

`all_objects` with an explicit filter rather than the ambient manager, so the
scoping is visible on the line that does it. The branch clause is `OR
branch IS NULL` for a reason recorded in the code: an exact-match filter left
branch users with an empty list whenever the tenant published at tenant level,
which is the normal case.

### Publish payload (`serializers.py:137`)

```json
{"scope": "TENANT" | "PLATFORM",
 "document_type": "finance.journal", "code": "standard", "name": "…",
 "notification_events": {"workflow.stage_activated": true},
 "stages": [ { "code": "…", "label": "…", "approver_source": "ROLE",
               "approver_role_key": "…", "advance_rule": "UNANIMOUS",
               "on_rejection": "TERMINAL", "skip_if_no_approvers": false,
               "inclusion_condition": {…} } ],
 "routes":  [ { "from_stage_code": null, "to_stage_code": "…",
                "order": 0, "condition": {…} } ]}
```

`validate_stages` (`serializers.py:152-218`) refuses an empty stage list, a
stage without `code`/`label`, any unknown enum value, and every
source-specific omission: a `ROLE` stage with no `approver_role_key`, a
`WORKFLOW_GROUP` stage with no `approver_group_code`, an `ORGANOGRAM` stage with
no `organogram_target` (or `SPECIFIC_POSITION` with no position code), a
`DYNAMIC_ROLE` stage with no rules.

## 4. Lifecycle

A template has no status beyond `is_active`:

```text
   POST publish (scope=PLATFORM, platform actor)
        └─► shared template            tenant = NULL, is_active = True
                 │
                 │  a tenant publishes its own (same document_type + code)
                 ▼
        tenant template                tenant = X,    is_active = True
                 │                              the cascade now prefers this
                 │  POST use-platform-version/
                 ▼
        tenant template                tenant = X,    is_active = False
                 │                              the cascade falls through again
                 │  publish the same key again
                 └─► back to is_active = True
```

Stages have their own soft lifecycle inside that: present in the payload →
live; absent → `retired_at` stamped; present again → un-retired
(`services/templates.py:238`, `"retired_at": None` in the upsert defaults).

## 5. Derivations

- **`publish_template` validates everything before writing anything**
  (`services/templates.py:172-185`): dynamic rules, stage inclusion conditions
  and route conditions are all parsed first, so a bad operator produces an error
  about the payload rather than about a half-built template. The whole function
  is `@transaction.atomic` and takes `select_for_update` on the template row.
- **A `ROLE` stage's role must exist, but only on a tenant template.**
  `_resolve_role` (`services/templates.py:30-61`) refuses a key naming no ACTIVE
  role in the tenant, and returns `None` for a central template because the
  tenants that will run it may not exist yet. `check_workflow_role_coverage`
  reports those gaps instead (`workflow_approvers` §11).
- **A `WORKFLOW_GROUP` stage cannot live on a central template at all**
  (`services/templates.py:80-83`): groups are tenant-owned, so a shared
  definition has no group to name.
- **The foreign key is an anchor, the key is the lookup.**
  `stage.approver_role` is set only for tenant templates and exists so `PROTECT`
  stops the role being deleted; resolution always goes through
  `approver_role_key` so one definition names the same authority everywhere.
- **`_counterparts`** (`views.py:176-202`) resolves the platform/tenant pairing
  for a whole page in one query, so the list can say which version each tenant
  is running without a query per row. An inactive tenant version is deliberately
  **not** reported as "mine": they asked for the platform's back.
- **`adoption` and `compare` are the only cross-tenant reads in the module**,
  and `_platform_oversight` (`views.py:318-340`) gates both: the caller's own
  tenant must be `PLATFORM` and the subject must be the shared template.
  `compare` additionally re-checks that the other id is an active tenant version
  of the same `(document_type, code)`, and answers the same `404` for "no such
  template" and "not a version of this one" so it cannot be used to probe ids.
- **`preview-approvers`** (`views.py:221-313`) builds a transient, unsaved stage
  and instance and runs the real resolver, so the builder's answer and the
  engine's answer come from the same code. `DYNAMIC_ROLE` is the exception: an
  unsaved stage cannot carry a reverse FK, so `_preview_dynamic_role`
  (`views.py:68-121`) evaluates the posted rules directly.

## 6. What writing writes

Only template configuration. Publishing writes no audit event anywhere: not to
`WorkflowAuditLog` (which is keyed on an instance), not to `vs_audit`.

| Operation | Writes |
|---|---|
| `publish` | The template row, upserted stages, replaced dynamic rules, replaced routes, retirement stamps on omitted stages |
| `use-platform-version` | `is_active = False` on the tenant's row |
| `adoption`, `compare`, `preview-approvers` | nothing |

**Nothing records who changed an approval path, or when, beyond
`updated_at` and `created_by`.** A stage's `advance_rule` can go from
`UNANIMOUS` to `ANY`, or `skip_if_no_approvers` from `False` to `True`, and the
only trace is that the row now says something different. For a module whose
whole subject is controlled approval, that is the gap recorded as
`workflow_code_issues.md` §12.

## 7. Worked example

Codex publishes the shared payout ladder once:

```text
POST /v1/workflow/templates/publish/?tenant=codex
{"scope": "PLATFORM", "document_type": "payments.payout_batch",
 "code": "standard", "name": "Payout approval",
 "stages": [
   {"code": "payout-approval", "label": "Payout approval",
    "approver_source": "ROLE", "approver_role_key": "payout-approver",
    "advance_rule": "ANY", "on_rejection": "TERMINAL",
    "skip_if_no_approvers": false}]}
```

`skip_if_no_approvers: false` is the whole control: if nobody holds
`payout-approver`, the batch parks rather than approving itself
(`workflow_parking_release`).

Bright Star wants two signatures instead of one. Their admin publishes their own
version of the same key:

```text
POST /v1/workflow/templates/publish/?tenant=bright-star
{"document_type": "payments.payout_batch", "code": "standard", … ,
 "stages": [{… "advance_rule": "QUORUM", "quorum_count": 2 …}]}
```

From the next submission on, Bright Star's batches run their template and every
other tenant still runs Codex's. Codex can see that:

```text
GET /v1/workflow/templates/<platform id>/adoption/?tenant=codex
  → how many tenants still follow the shared path, and who has their own

GET /v1/workflow/templates/<platform id>/compare/?tenant=codex&with=<bright-star id>
  → {"stages": [{"code": "payout-approval",
                 "differences": [{"field": "advance_rule",
                                  "label": "Advance rule",
                                  "base": "ANY", "other": "QUORUM"}, …]}]}
```

If Bright Star later decides the shared path was fine:

```text
POST /v1/workflow/templates/<bright-star id>/use-platform-version/?tenant=bright-star
  → their row is switched off, the response is the platform template
```

and if no platform version existed, the answer is `409 NO_PLATFORM_VERSION`
rather than leaving the document type with no template at all
(`views.py:441-451`).

## 8. Gotchas / known limitations

Full evidence in **`error/workflow/workflow_code_issues.md`**. The items
belonging to this slice:

- **Template changes are not audited.** Nothing records who altered an approval
  path or what it said before (`workflow_code_issues.md` §12).
- **Detail responses are not wrapped in the platform envelope.** The viewsets use
  DRF's mixins, not `core.mixins`, so `GET templates/<id>/` returns a bare object
  while `GET templates/` returns `{success, pagination, data}`
  (`workflow_code_issues.md` §13).
- **`notification_events` keys are never validated** against
  `NOTIF_EVENT_KEYS`, and a non-empty dict is treated as exact intent - so one
  typo silences every notification the template would have sent
  (`workflow_code_issues.md` §4).
- **`WorkflowRoutePath` has no unique constraint on `(from_stage, order)`**, so
  two routes can claim the same evaluation position
  (`workflow_code_issues.md` §16).
- **`_filter_by_branch` (`views.py:54-66`) is dead code** - its logic was
  inlined into `get_queryset` and the helper is called from nowhere
  (`workflow_code_issues.md` §16).
- **A tenant that can publish can flatten its own approval ladder.** A template
  whose only stage is a `BRANCH` node, or whose approval stages all carry
  conditions the document fails, routes straight to APPROVED - and
  `template_requires_approval` answers False, so the finance direct-post gate
  lets the document skip approval entirely. There is no platform floor
  (`workflow_code_issues.md` §11). Today this needs `workflow.template.manage`,
  which is seeded to platform roles only.
- **Publishing is not idempotent for routes and rules**: both are deleted and
  recreated on every publish, so their ids change even when nothing did.
- **Justified by design:** stages are retired rather than deleted, and a tenant
  version is switched off rather than deleted - in both cases because live
  instances hold `PROTECT` references.
- **Justified by design:** `compare` answers one `404` for both "no such id" and
  "not a version of this template".

## 9. Permissions & tenant isolation

| Surface | Key | Seeded to |
|---|---|---|
| List, retrieve, preview-approvers | `workflow.template.view` | `xvs_super_admin`, `xvs_platform_admin` |
| Publish, use-platform-version, adoption, compare | `workflow.template.manage` (`SENSITIVE`) | the same two |

Both are `PermissionScope.TENANT`
(`management/commands/seed_workflow_permissions.py:107`), so a school role could
legally hold either if somebody minted one - the scope guard would not stop it.
Nothing in the repo does today.

Isolation is `Q(tenant=request.tenant) | Q(tenant__isnull=True)` on every read,
plus the `_platform_oversight` gate on the two cross-tenant endpoints. A
tenant's own template is invisible to every other tenant, including the
platform's ordinary list - the platform reaches it only through `compare`, and
only for the shared template it already owns.

`preview-approvers` is worth one caution: it takes an arbitrary `requester` user
id (`views.py:230-234`) and resolves approvers **in that user's tenant**. It is
gated on `workflow.template.view`, which is platform-only, so the cross-tenant
read it performs is deliberate. Any future grant of that key to a school role
would make it a cross-tenant people-lookup.

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py:45-124` | `WorkflowTemplate` |
| `models.py:246-342` | `WorkflowStage` |
| `models.py:413-461` | `WorkflowStageDynamicRule` |
| `models.py:464-493` | `WorkflowRoutePath` |
| `services/templates.py:14-153` | `_resolve_position`, `_resolve_role`, `_resolve_group`, `_parse_dynamic_rules` |
| `services/templates.py:156-275` | `publish_template` |
| `services/templates.py:278-287` | `active_instances_for_template` |
| `services/comparison.py` | `adoption_for`, `compare_templates`, the field lists the diff reports |
| `views.py:145-219` | `WorkflowTemplateViewSet` - queryset, counterparts |
| `views.py:221-313` | `preview_approvers` |
| `views.py:318-397` | `_platform_oversight`, `adoption`, `compare` |
| `views.py:400-467` | `publish`, `use_platform_version` |
| `serializers.py:21-218` | Read serializers and `WorkflowTemplatePublishSerializer` |
| `conditions/evaluator.py:114-164` | `validate_condition` - what publishing rejects |

## 11. Test coverage & gaps

- `PublishTemplateTests` (`tests/test_services.py:215-314`) - create, republish
  in place, soft-retire a removed stage, re-activate it by republishing, and
  routes replaced entirely.
- `PublishRoleStageTests` (`480-537`) - the key is stored and anchored; unknown,
  missing and inactive role keys each fail the publish; a central template keeps
  the key without a tenant.
- `PublishGroupStageTests` (`735-785`) - group code resolved to the FK, and five
  refusals including another tenant's group and a global template using a group.
- `PublishDynamicRoleTests` (`955-1070`) - rules persisted in evaluation order,
  republish replaces them, empty rules rejected, bad operator / missing field /
  non-list `in` value rejected at publish, a rule after the fallback rejected,
  and stale rules dropped when a stage stops being dynamic.
- `PlatformTemplateTests` (`tests/test_platform_templates.py`) - a tenant cannot
  publish a shared template; a platform actor can; the tenant version wins and
  falls back when switched off; reset refuses with no platform version and on
  the platform template itself; publishing again brings a switched-off version
  back; and the list reports which version each tenant runs.
- `TemplateScopingTests` (`tests/test_tenant_scoping.py:175-210`) - the list
  excludes other tenants but keeps global rows, another tenant's detail is a
  `404`, and a branch user still sees tenant-wide templates.

This is the best-covered part of the module. What it does not cover:

1. **`compare` and `adoption` as endpoints** - `comparison.py`'s output shape is
   exercised through `test_platform_change_after_a_tenant_adjusted_is_flagged`,
   but neither the `PLATFORM_ONLY` refusal nor the `NOT_PLATFORM_TEMPLATE`
   refusal is asserted.
2. **`notification_events`** - no test publishes one, valid or invalid.
3. **A route with no matching condition**, and two routes sharing an `order`.
4. **The response envelope.** Nothing asserts the shape of a detail response, so
   the inconsistency in §8 is invisible to the suite.
5. **`preview-approvers` for `ORGANOGRAM` and `WORKFLOW_GROUP`** - only the
   `ROLE` and `DYNAMIC_ROLE` paths are tested
   (`tests/test_approver_groups_api.py:349-437`, `test_services.py:539-566`).
6. **Publishing at branch scope** - `get_branch()` feeds `branch=` on every
   tenant publish and no test publishes as a branch user.
