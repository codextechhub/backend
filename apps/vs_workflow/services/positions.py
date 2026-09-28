"""Named posts on an organogram, and which organogram a row's posts come from.

Two things in the engine name one post: a stage whose organogram target is
SPECIFIC_POSITION, and an approver group member of kind POSITION. Both carry
the post in one of two columns, and never both:

* the ``vs_user.Position`` foreign key, for a post on the CX chart;
* a plain id (``organogram_tenant_position_id`` on a stage,
  ``tenant_position_id`` on a group member), for a post on a tenant's own chart.

**The tenant that owns the row decides which.** A central template (no tenant)
and anything the platform tenant owns name a seat on the CX chart. Any other
tenant names a post on the chart registered for its kind with
:func:`register_tenant_organogram`, looked up and resolved inside that tenant
only. A tenant kind with no registered chart cannot name a post at all, and is
told so rather than having the reference quietly dropped.

The tenant's chart is referenced by id rather than by a foreign key because the
engine never imports the app that keeps it. That app registers the chart as it
loads, and it is also the one that refuses to delete a post still named here,
by asking :func:`position_references`.

The code a caller types is only the lookup key. Codes are editable on a tenant's
chart, so the id is what is stored, and every read shows the post's current
code and title.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from vs_workflow.exceptions import UnknownPositionError

logger = logging.getLogger(__name__)


#: The organogram each tenant kind uses, registered by the app that owns it.
#:
#: The engine never imports a domain app, so a tenant's own org chart reaches it
#: the way a document handler does: the app that keeps the chart registers it
#: here as it loads. See :func:`register_tenant_organogram`.
_TENANT_ORGANOGRAMS: dict = {}


def register_tenant_organogram(tenant_kind: str, organogram) -> None:
    """Name the organogram tenants of *tenant_kind* climb and name posts on.

    *organogram* answers, always inside the tenant it is given and never
    returning the requester:

    * ``resolve_direct_manager(user, tenant)``,
      ``resolve_n_levels_up(user, levels, tenant)`` and
      ``resolve_department_head(user, tenant)``: the three climbs, as users;
    * ``find_position(code, tenant)``: ``(id, code, title)`` of the active post
      with that code, or None;
    * ``describe_positions(ids, tenant)``: ``{id: (code, title)}`` for the posts
      among *ids* that belong to the tenant, in one query;
    * ``resolve_position_holders(id, tenant, exclude_user=None)``: the users
      holding the post who can act on a document today.

    Registering again replaces the earlier entry, so an app whose ``ready`` runs
    twice at startup does not fail.
    """
    _TENANT_ORGANOGRAMS[tenant_kind] = organogram


def uses_platform_chart(tenant) -> bool:
    """Whether a row owned by *tenant* names its posts on the CX chart."""
    if tenant is None:
        return True
    from vs_tenants.models import Tenant

    return getattr(tenant, "kind", None) == Tenant.Kind.PLATFORM


def tenant_organogram(tenant):
    """The chart registered for *tenant*'s kind, or None."""
    return _TENANT_ORGANOGRAMS.get(getattr(tenant, "kind", None))


def as_tenant(tenant):
    """*tenant* as a model instance, when a caller holds only its id."""
    if tenant is None or hasattr(tenant, "kind"):
        return tenant
    from vs_tenants.models import Tenant

    return Tenant.objects.filter(pk=tenant).first()


@dataclass(frozen=True)
class BoundPosition:
    """One post a caller named, ready to store on a stage or a group member.

    Exactly one of ``position`` (a ``vs_user.Position``) and
    ``tenant_position_id`` is set, following :func:`uses_platform_chart`.
    """

    position: object
    tenant_position_id: Optional[int]
    code: str
    title: str


def bind_position(code: str, tenant, *, where: str = "", active_only: bool = True) -> BoundPosition:
    """The post *code* names on the chart a row owned by *tenant* uses.

    Raises :class:`~vs_workflow.exceptions.UnknownPositionError` naming the
    code when that chart has no such post, and when *tenant*'s kind has no chart
    to name one on. A post on the other chart is never a fallback: a tenant row
    given a CX seat's code is refused exactly like a code nobody has used, and
    so is the reverse.

    ``active_only`` applies to the CX chart. A tenant's chart only ever offers
    its active posts, which is what ``find_position`` answers.
    """
    def refuse(text):
        text = f"{where}: {text}" if where else text[:1].upper() + text[1:]
        return UnknownPositionError(text, position_code=code)

    code = (code or "").strip()
    if not code:
        raise refuse("a position code is required.")

    if uses_platform_chart(tenant):
        from vs_user.models import Position

        seats = Position.objects.filter(code=code)
        if active_only:
            seats = seats.filter(is_active=True)
        seat = seats.first()
        if seat is None:
            raise refuse(f"no position with code '{code}' exists on the platform organogram.")
        return BoundPosition(position=seat, tenant_position_id=None,
                             code=seat.code, title=seat.title)

    organogram = tenant_organogram(tenant)
    if organogram is None:
        raise refuse(f"this tenant has no organogram to name position '{code}' on.")
    found = organogram.find_position(code, tenant)
    if found is None:
        raise refuse(f"no active post with code '{code}' exists on this organogram.")
    position_id, found_code, title = found
    return BoundPosition(position=None, tenant_position_id=position_id,
                         code=found_code, title=title)


def resolve_tenant_position_holders(position_id, tenant, exclude_user=None) -> list:
    """Holders of a post on *tenant*'s own chart, or nobody when it has none."""
    organogram = tenant_organogram(tenant)
    if organogram is None:
        logger.warning(
            "No organogram registered for tenant kind %s; a named post resolved "
            "to no approvers.", getattr(tenant, "kind", None))
        return []
    return organogram.resolve_position_holders(position_id, tenant, exclude_user=exclude_user)


def describe_tenant_positions(pairs: Iterable[Tuple[object, int]]) -> Dict[Tuple[object, int], Tuple[str, str]]:
    """``{(tenant_id, position_id): (code, title)}`` for tenant-chart posts.

    *pairs* are ``(tenant_id, position_id)``. One query for the tenants and one
    per tenant for their posts, so a page of stages or group members costs the
    same whatever its length. A post that no longer exists, or that is not the
    named tenant's, is absent from the answer.
    """
    wanted: Dict[object, set] = {}
    for tenant_id, position_id in pairs:
        if tenant_id is not None and position_id is not None:
            wanted.setdefault(tenant_id, set()).add(position_id)
    if not wanted:
        return {}

    from vs_tenants.models import Tenant

    labels: Dict[Tuple[object, int], Tuple[str, str]] = {}
    for tenant in Tenant.objects.filter(pk__in=list(wanted)):
        organogram = tenant_organogram(tenant)
        if organogram is None:
            continue
        for position_id, label in organogram.describe_positions(
                sorted(wanted[tenant.pk]), tenant).items():
            labels[(tenant.pk, position_id)] = label
    return labels


def describe_position(position, tenant_position_id, tenant) -> Optional[Tuple[str, str]]:
    """``(code, title)`` of whichever post a row names, or None when it names none."""
    if position is not None:
        return position.code, position.title
    if tenant_position_id is None or tenant is None:
        return None
    organogram = tenant_organogram(tenant)
    if organogram is None:
        return None
    return organogram.describe_positions([tenant_position_id], tenant).get(tenant_position_id)


def position_references(tenant, tenant_position_id) -> Dict[str, int]:
    """How many live stages and approver group members name one tenant post.

    The hook the app keeping *tenant*'s chart calls before deleting a post. The
    engine stores the post as a plain id, so nothing in the database stops the
    delete; this is what does. Retired stages are history and are not counted,
    the same rule an approver group's own delete refusal applies.
    """
    from vs_workflow.models import WorkflowApproverGroupMember, WorkflowStage

    stages = WorkflowStage.objects.filter(
        organogram_tenant_position_id=tenant_position_id,
        template__tenant=tenant, retired_at__isnull=True,
    ).count()
    members = WorkflowApproverGroupMember.objects.filter(
        tenant_position_id=tenant_position_id, group__tenant=tenant,
    ).count()
    return {"workflow_stages": stages, "approver_group_members": members}
