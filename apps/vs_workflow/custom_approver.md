# vs_workflow — Implementation Notes

---

## Approver Permission Key: RBAC vs Organogram

### Decision: Keep the current RBAC permission-based approach

For a platform that handles multiple document types — procurement, leave, HR,
admissions — the flows are too varied for a single organogram model. Purchase
orders go to Finance, not the requester's manager. Legal sign-off goes to Legal
regardless of where in the hierarchy the request originated. The RBAC approach
handles all of these cleanly.

The organogram scenario is supported as a **special case** via a custom approver
resolver — see the section below.

---

## Custom Approver Resolvers (e.g. `org.direct_manager`)

### The idea

Today every `WorkflowStage` has an `approver_permission_key` — a string like
`"leave.approve.line_manager"`. The engine passes that string to `vs_rbac` to
find who holds that role.

For organogram-style flows you don't want a role lookup — you want to walk the
org chart: *"the person directly above the requester is the approver."* You can
do this inside the existing engine without changing any models or the template
API. Register a **custom approver resolver** under a special key and the engine
calls your function instead of RBAC when it sees that key on a stage.

The pattern mirrors `@register_condition` and `@register_handler` already in
the engine.

---

### Step 1 — Add the resolver registry

Create `vs_workflow/approver_resolvers/` as a new sub-package.

**`vs_workflow/approver_resolvers/__init__.py`**
```python
from vs_workflow.approver_resolvers.registry import (
    register_approver_resolver,
    get_approver_resolver,
    has_approver_resolver,
)

__all__ = [
    "register_approver_resolver",
    "get_approver_resolver",
    "has_approver_resolver",
]
```

**`vs_workflow/approver_resolvers/registry.py`**
```python
"""Registry for custom approver resolver functions."""
from typing import Callable, Dict, Optional

_REGISTRY: Dict[str, Callable] = {}


def register_approver_resolver(key: str):
    """
    Decorator — register a function as the approver resolver for a given key.

    The decorated function must accept (stage, instance) and return a list of
    EligibleApprover dataclasses (same type as services/approvers.py produces).

    Example:
        @register_approver_resolver("org.direct_manager")
        def resolve_direct_manager(stage, instance):
            ...
            return [EligibleApprover(user=manager)]
    """
    def _decorate(fn: Callable):
        if key in _REGISTRY:
            raise ValueError(
                f"Approver resolver '{key}' is already registered. "
                "Each key may only have one resolver."
            )
        _REGISTRY[key] = fn
        return fn
    return _decorate


def get_approver_resolver(key: str) -> Optional[Callable]:
    return _REGISTRY.get(key)


def has_approver_resolver(key: str) -> bool:
    return key in _REGISTRY
```

---

### Step 2 — Plug the registry into `services/approvers.py`

Update `resolve_approvers` to check the registry first. If the key is
registered there, call the custom function and skip RBAC entirely.

```python
# add at the top of vs_workflow/services/approvers.py
from vs_workflow.approver_resolvers import get_approver_resolver, has_approver_resolver


def resolve_approvers(stage, instance):
    if not stage.approver_permission_key:
        return []

    # Custom resolver takes full control — bypasses RBAC and delegation expansion.
    if has_approver_resolver(stage.approver_permission_key):
        resolver = get_approver_resolver(stage.approver_permission_key)
        return resolver(stage, instance)

    # Default path — RBAC role lookup + delegation expansion (unchanged).
    base_qs = _users_with_permission(
        school=instance.school,
        branch=instance.branch,
        permission_key=stage.approver_permission_key,
        scope=ApproverScope(stage.approver_scope),
    )
    base_qs = base_qs.exclude(pk=instance.requested_by_id)
    base_users = list(base_qs.distinct())
    base_ids = {u.pk for u in base_users}
    # ... rest of delegation logic unchanged ...
```

> **Note:** Custom resolvers own their list completely. Delegation expansion and
> requester-exclusion are skipped — the resolver is responsible for those rules
> if they apply.

---

### Step 3 — Auto-discover resolver files on startup

Add one line to `VsWorkflowConfig.ready()` in `apps.py`:

```python
def ready(self):
    from vs_workflow import signals  # noqa: F401
    autodiscover_modules("workflow_handlers")
    autodiscover_modules("workflow_conditions")
    autodiscover_modules("workflow_approver_resolvers")   # ← add this
```

Django will scan every installed app for `workflow_approver_resolvers.py` and
import it, triggering the `@register_approver_resolver` decorators.

---

### Step 4 — Write the resolver in your feature app

Create `workflow_approver_resolvers.py` inside the app that owns the org chart
(e.g. `vs_hr`).

```python
# vs_hr/workflow_approver_resolvers.py

from vs_workflow.approver_resolvers import register_approver_resolver
from vs_workflow.services.approvers import EligibleApprover


@register_approver_resolver("org.direct_manager")
def resolve_direct_manager(stage, instance):
    """
    Returns the requester's direct manager as the sole eligible approver.
    Reads the manager relationship from StaffProfile in vs_hr.
    """
    from vs_hr.models import StaffProfile

    try:
        profile = StaffProfile.objects.select_related("manager").get(
            user=instance.requested_by
        )
    except StaffProfile.DoesNotExist:
        return []

    manager = profile.manager
    if manager is None or manager.pk == instance.requested_by_id:
        return []

    return [EligibleApprover(user=manager)]


@register_approver_resolver("org.skip_one_manager")
def resolve_skip_one_manager(stage, instance):
    """
    Returns the requester's manager's manager (one level higher).
    Used for high-value approvals that bypass the direct line.
    """
    from vs_hr.models import StaffProfile

    try:
        profile = StaffProfile.objects.select_related(
            "manager__staffprofile__manager"
        ).get(user=instance.requested_by)
    except StaffProfile.DoesNotExist:
        return []

    direct = profile.manager
    if direct is None:
        return []

    try:
        senior = direct.staffprofile.manager
    except StaffProfile.DoesNotExist:
        return []

    if senior is None or senior.pk == instance.requested_by_id:
        return []

    return [EligibleApprover(user=senior)]
```

---

### Step 5 — Use the resolver key in a template

Use the resolver key as `approver_permission_key`. Nothing else changes.

```json
POST /v1/workflow/templates/publish/
{
  "document_type": "leave.request",
  "code": "standard",
  "name": "Standard Leave Approval",
  "stages": [
    {
      "code": "direct-manager",
      "label": "Direct Manager Approval",
      "kind": "APPROVAL",
      "order": 1,
      "approver_permission_key": "org.direct_manager",
      "approver_scope": "SCHOOL",
      "advance_rule": "ANY",
      "on_rejection": "RETURN_TO_REQUESTER",
      "skip_if_no_approvers": false
    },
    {
      "code": "senior-manager",
      "label": "Senior Manager Sign-off",
      "kind": "APPROVAL",
      "order": 2,
      "approver_permission_key": "org.skip_one_manager",
      "approver_scope": "SCHOOL",
      "advance_rule": "ANY",
      "on_rejection": "TERMINAL",
      "skip_if_no_approvers": true
    }
  ],
  "routes": []
}
```

The engine sees `"org.direct_manager"` on stage 1, finds it in the resolver
registry, calls your function, gets back the manager. No RBAC query, no engine
changes. Audit logs, notifications, stage instances, and delegation all work
as normal.

---

### Mixing both in one template

Both resolver types can coexist in the same template. A leave request could
have stage 1 resolved by organogram and stage 2 resolved by RBAC:

```json
"stages": [
  {
    "code": "direct-manager",
    "approver_permission_key": "org.direct_manager",   ← organogram
    "order": 1
  },
  {
    "code": "hr-final",
    "approver_permission_key": "leave.approve.hr",     ← RBAC role
    "order": 2
  }
]
```

---

### Comparison

| | RBAC permission key | Custom resolver key |
|---|---|---|
| Example | `"leave.approve.finance"` | `"org.direct_manager"` |
| Resolves to | Anyone holding that RBAC role | Whoever your function returns |
| Where logic lives | `vs_rbac` role assignments | `workflow_approver_resolvers.py` in your app |
| Delegation expansion | Automatic | Handle it in your function if needed |
| Best for | Role-based authority | Org chart / relationship-based authority |
