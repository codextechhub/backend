"""The search bar's one endpoint."""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_config.services.capabilities import self_effective_capabilities
from vs_config.services.scopes import resolve_request_scope
from vs_rbac.evaluator import get_effective_permissions
from vs_rbac.permissions import IsAuthenticatedAndActive, is_vision_super_admin

from . import registry
from .matching import rank_places

#: Below this, a query is a keystroke rather than a search.
MIN_QUERY = 2
#: How many hits each group carries at most.
PEOPLE_LIMIT = 10
ACTION_LIMIT = 10
SETTING_LIMIT = 10

#: The order the groups are shown in, top to bottom.
GROUP_ORDER = ("people", "actions", "settings", "guides")


def empty_results() -> dict:
    """The response shape with every group empty."""
    return {"order": list(GROUP_ORDER), **{name: [] for name in GROUP_ORDER}}


def visible_places(user, *, permission_tenant, scope_tenant, branch):
    """Every registered place this caller may open, before conditions, in no order.

    A place is visible when the caller holds one of its permission keys (or it
    needs none) and the module it belongs to is switched on for the tenant. The
    permissions are evaluated once and every place is tested against the set.
    A platform super administrator passes both checks, as they pass both at
    every endpoint's door. The condition a place names is asked later, by
    :func:`meeting_conditions`, and only of the places a query matched.
    """
    candidates = registry.places()
    if is_vision_super_admin(user):
        return candidates
    held = get_effective_permissions(user, tenant=permission_tenant)
    enabled = None
    out = []
    for place in candidates:
        keys = place.permission_keys
        if keys and not any(key in held for key in keys):
            continue
        if place.capability is not None:
            if enabled is None:
                enabled = {
                    row["key"]
                    for row in self_effective_capabilities(tenant=scope_tenant, branch=branch)
                    if row["enabled"]
                }
            if place.capability not in enabled:
                continue
        out.append(place)
    return out


def meeting_conditions(places, *, request, tenant, branch, user):
    """*places*, in order, without those whose condition does not hold for this caller.

    Each condition is asked at most once per call, and only when one of
    *places* names it, so a search whose matches are all unconditional asks
    none. A super administrator is held to conditions like anybody else: a
    screen a one-branch school does not have is not there for anybody.
    """
    answers: dict[str, bool] = {}
    out = []
    for place in places:
        name = place.condition
        if name is not None:
            if name not in answers:
                answers[name] = bool(registry.condition(name)(
                    request=request, tenant=tenant, branch=branch, user=user,
                ))
            if not answers[name]:
                continue
        out.append(place)
    return out


def find_people(request, *, tenant, query):
    """People hits from every provider, in registration order, capped."""
    people: list[dict] = []
    for provider in registry.people_providers():
        if len(people) >= PEOPLE_LIMIT:
            break
        people.extend(provider(
            request=request, tenant=tenant, user=request.user,
            query=query, limit=PEOPLE_LIMIT,
        ))
    return people[:PEOPLE_LIMIT]


def _action_hit(place) -> dict:
    return {
        "key": place.key,
        "label": place.label,
        "description": place.description,
        "screen": place.screen,
    }


def _setting_hit(place) -> dict:
    return {
        "key": place.key,
        "label": place.label,
        "description": place.description,
        "group": place.group,
        "screen": place.screen,
        "anchor": place.anchor,
    }


class SearchView(APIView):
    """GET /v1/search/?q= - people, screens and settings matching a query.

    One answer for the search bar, in four groups whose order is fixed by
    ``order``: people first, then actions (screens), then settings, then guides.
    ``guides`` is always an empty list here. Guide content lives in the
    frontend (the support app holds only guide analytics), so the frontend
    fills that group itself; the empty slot is what fixes where it goes.

    A query shorter than two characters answers every group empty, because one
    character is a keystroke, not a search.

    People come from the module that owns them (students, guardians, staff, in
    that order), each scoped, permission-checked and Field-Access-filtered by
    that module exactly as its own search endpoint is, at most ten in all.
    Screens and settings are registered in code by the app that owns each one
    (:mod:`vs_search.registry`) and are offered only where the caller holds the
    key the screen's own endpoint requires and the tenant's plan includes the
    module, and, for a screen that exists only sometimes (several branches,
    a whole-tenant reader, one custody mode), only where it exists for this
    caller. At most ten of each. A hit names where it routes (``screen`` and,
    for a setting, ``anchor``) and never which permission or capability
    admitted it.

    No permission key of its own: everything it returns the caller could
    already open. The tenant comes from the assertion the authentication layer
    has validated, so naming another school's slug is refused before this view
    runs. Open while a tenant is pending, because a school sets itself up then.

    docstring-name: Search
    """

    permission_classes = [IsAuthenticatedAndActive]
    tenant_param_required = False
    pending_tenant_surface = ("get",)

    def get(self, request):
        query = (request.query_params.get("q") or "").strip()
        if len(query) < MIN_QUERY:
            return success_response("Search results.", empty_results())

        scope_tenant, branch = resolve_request_scope(request)
        permission_tenant = (
            getattr(request, "rbac_tenant", None)
            or getattr(request, "tenant", None)
            or getattr(request.user, "tenant", None)
        )

        data = empty_results()
        if scope_tenant is not None:
            data["people"] = find_people(request, tenant=scope_tenant, query=query)

        ranked = meeting_conditions(
            rank_places(
                visible_places(
                    request.user, permission_tenant=permission_tenant,
                    scope_tenant=scope_tenant, branch=branch,
                ),
                query,
            ),
            request=request, tenant=scope_tenant, branch=branch, user=request.user,
        )
        data["actions"] = [
            _action_hit(p) for p in ranked if p.kind == registry.ACTION
        ][:ACTION_LIMIT]
        data["settings"] = [
            _setting_hit(p) for p in ranked if p.kind == registry.SETTING
        ][:SETTING_LIMIT]
        return success_response("Search results.", data)
