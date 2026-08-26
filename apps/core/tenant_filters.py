"""Resolving a "show me this customer's rows" filter, without touching ``?tenant=``.

``?tenant=`` is not available for this. It is the **tenant assertion** read by
``vs_rbac.authentication.TenantJWTAuthentication``: it names the tenant the
caller is acting in, it is required on nearly every endpoint, and a platform
actor naming a tenant that is not their own is refused with 404 unless the view
sets ``platform_cross_tenant_param``.

Two live defects came from reading it as a row filter anyway:

* ``vs_health.TaskListView`` filtered ``tenant_id=<the asserted slug>``. The
  column is an integer foreign key and the assertion is a slug, so the query
  raised and the Jobs & Queues task table answered 500 to every caller it ever
  had. No value of ``?tenant=`` could satisfy both readers: the assertion
  demands a slug, the filter demands a pk.
* ``vs_admin_console.TaskMonitorViewSet`` resolved the same parameter to a
  tenant and narrowed by it. That one did not raise, which made it worse: a
  Super Admin holding ``platform.tasks.view_all`` sends the mandatory
  ``?tenant=codex`` like everybody else, so the platform-wide list they hold a
  CRITICAL key for silently collapsed to Codex's own system jobs and showed no
  school at all.

So the filter gets its own name. ``?for_tenant=`` reads as what it is - whose
rows, not who is asking - and cannot collide with the assertion whatever the
view's cross-tenant flags say.
"""
from __future__ import annotations

#: Query parameter naming whose rows the caller wants. Deliberately NOT
#: ``tenant``; see the module docstring.
PARAM = "for_tenant"


def requested_tenant(params, param: str = PARAM):
    """Resolve ``?for_tenant=`` to a Tenant, or report that it named none.

    Returns ``(asked, tenant)``:

    * ``(False, None)`` - the caller did not ask, so do not narrow.
    * ``(True, <Tenant>)`` - narrow to this one.
    * ``(True, None)`` - the caller asked for something that does not exist.
      The caller must narrow to nothing rather than ignore it: silently
      returning every tenant for a mistyped slug is how a filter becomes a
      leak.

    Accepts a numeric pk or a slug, because the two identify a tenant in
    different halves of this platform and a caller should not have to know
    which one a given screen holds.
    """
    from vs_tenants.models import Tenant

    raw = (params.get(param) or "").strip()
    if not raw or raw.lower() == "all":
        return False, None

    lookup = {"pk": int(raw)} if raw.isdigit() else {"slug": raw.lower()}
    return True, Tenant.objects.filter(**lookup).first()


def narrow(qs, params, field: str = "tenant", param: str = PARAM):
    """Apply :func:`requested_tenant` to *qs* on *field*."""
    asked, tenant = requested_tenant(params, param)
    if not asked:
        return qs
    return qs.filter(**{field: tenant}) if tenant else qs.none()
