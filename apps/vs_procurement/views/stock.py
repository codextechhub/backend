"""Perpetual-inventory masters, immutable movements, and valuation reports.

Master endpoints never patch balances.  Issues and adjustments cross the stock
service boundary, which owns row locking, moving-average costing, availability,
and journal creation.  Quantities are bounded decimals; monetary unit costs and
values are integer kobo.
"""
from __future__ import annotations

import datetime

from django.db import IntegrityError, transaction
from django.db.models import (
    BigIntegerField, Count, DecimalField, F, OuterRef, Q, Subquery, Sum, Value,
)
from django.db.models.functions import Coalesce
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.money import format_naira
from vs_finance.views import resolve_entity

from .. import stock
from ..models import (
    StockBalance,
    StockItem,
    StockLocation,
    StockMovement,
)
from ..serializers import (
    StockBalanceSerializer,
    StockItemDetailSerializer,
    StockItemListSerializer,
    StockLocationSerializer,
    StockMovementSerializer,
)


from .base import (
    _branch_scope,
    _catalogue_or_404,
    _catalogue_visible,
    _kobo,
    _raised_branch,
    _ProcBase,
    _date,
    _nonneg_qty,
    _quantity,
    _resolve_asset_account,
    _resolve_expense_account,
    _signed_qty,
    _strict_kobo,
    _text,
)
from .catalog import _resolve_catalog_item

# --------------------------------------------------------------------------- #
# Inventory / stock ledger                                                     #
# --------------------------------------------------------------------------- #


def _strict_bool(value, field):
    """Require a real JSON boolean instead of accepting truthy strings or numbers."""
    if not isinstance(value, bool):
        raise ValidationError({field: "Enter a valid boolean value."})
    return value


def _readable_balances(request, entity, location=None):
    """The balance rows one caller may read, narrowed to a store when they name one.

    Stock sits in a location and a location names a branch, so the branch rule the
    rest of procurement reads under reaches these rows through ``location__``. It is
    the catalogue reading of a null branch rather than the document one: a central
    store belongs to the whole school, so what stands in it is everybody's to see,
    and withholding it would leave a branch storekeeper looking at an empty screen
    instead of at their own stock.
    """
    qs = _catalogue_visible(
        request, StockBalance.objects.filter(stock_item__entity=entity),
        prefix="location__",
    )
    return qs if location is None else qs.filter(location=location)


def _readable_movements(request, entity):
    """The stock ledger one caller may read, by the store each movement happened at.

    A movement carrying no location belongs to no store, so it reads as shared with
    every branch rather than as another branch's.
    """
    return _catalogue_visible(
        request, StockMovement.objects.filter(entity=entity), prefix="location__",
    )


def _held_balances(request, entity, location=None):
    """The balances an item figure is summed from, or ``None`` for the entity total.

    ``None`` says the read covers the whole entity: a caller entitled to every store
    who names none is asking about the school, and the item's own roll-up already
    carries that figure, so nothing is summed and the answer is the one they have
    always had - which is every caller in a school nobody is pinned to a branch in.
    Anyone else is asking about part of the entity, so the balances become the
    population and each figure is summed from them.
    """
    if location is None and not _branch_scope(request).is_narrowed:
        return None
    return _readable_balances(request, entity, location)


def _summed_figures(held, stock_item_ids):
    """One pseudo-balance per item, summing what it holds across the stores in scope.

    Every id asked about is answered, so an item held only where this caller cannot
    see reports nothing rather than falling back to the entity roll-up carried on the
    item row. These rows are never saved: they exist to carry two numbers into the
    serializer overlay.
    """
    totals = {
        row["stock_item_id"]: row
        for row in held.filter(stock_item_id__in=stock_item_ids)
        .values("stock_item_id")
        .annotate(qty=Sum("on_hand_qty"), value=Sum("stock_value"))
    }
    return {
        item_id: StockBalance(
            stock_item_id=item_id,
            on_hand_qty=(totals.get(item_id) or {}).get("qty") or 0,
            stock_value=(totals.get(item_id) or {}).get("value") or 0,
        )
        for item_id in stock_item_ids
    }


def _below_reorder(held):
    """Ids of the items whose stock across ``held`` is at or below the reorder level.

    Measured against the total in scope rather than against each store's own row: a
    school holding four hundred at one branch and none at another is not short of
    them, and the reorder level is a policy per item rather than per shelf.
    """
    return (
        held.values("stock_item_id", "stock_item__reorder_level")
        .annotate(held_qty=Sum("on_hand_qty"))
        .filter(held_qty__lte=F("stock_item__reorder_level"))
        .values("stock_item_id")
    )


def _scoped_item_totals(held):
    """Annotations carrying what each item holds across the stores in ``held``.

    Correlated subqueries rather than a join, because the counts computed beside
    them are counts of items: joining the balances in would multiply an item by the
    number of stores holding it before anything had been counted.
    """
    quantity = DecimalField(max_digits=16, decimal_places=4)
    per_item = held.filter(stock_item=OuterRef("pk")).values("stock_item")
    return {
        "held_qty": Coalesce(
            Subquery(
                per_item.annotate(total=Sum("on_hand_qty")).values("total")[:1],
                output_field=quantity,
            ),
            Value(0, output_field=quantity),
        ),
        "held_value": Coalesce(
            Subquery(
                per_item.annotate(total=Sum("stock_value")).values("total")[:1],
                output_field=BigIntegerField(),
            ),
            Value(0, output_field=BigIntegerField()),
        ),
    }


def _stock_detail(request, entity, pk):
    """One stock item plus its newest 50 movement rows, bounded in SQL.

    The item is a single detail record, so one explicit limited ledger query is both
    simpler and stricter than prefetching an unbounded reverse relation then slicing in
    Python. Joined actor/item data keeps movement serialization query-flat.

    The ledger is the caller's own stores', so a storekeeper opening an item reads
    what moved where they work rather than another branch's receipts and issues
    listed under the same item.
    """
    item = (
        StockItem.objects
        .filter(entity=entity, pk=pk)
        .select_related("inventory_account", "default_expense_account", "catalog_item")
        .first()
    )
    if item is not None:
        item._recent_movements = list(
            _readable_movements(request, entity).filter(stock_item=item)
            .select_related("created_by", "stock_item")
            .order_by("-id")[:50]
        )
    return item


def _detail_payload(request, entity, pk):
    """The serialised detail record for one item, in the scope its reader may see.

    Header and ledger are scoped together deliberately: quantity, value and unit cost
    describing the whole school while the movements listed below them describe one
    store is the contradiction the store filter on the list already exists to avoid.
    """
    item = _stock_detail(request, entity, pk)
    if item is None:
        raise NotFound("No such stock item in this entity.")
    held = _held_balances(request, entity)
    context = {} if held is None else {
        "location_balances": _summed_figures(held, [item.pk]),
    }
    return StockItemDetailSerializer(item, context=context).data


def _flag(raw, field, default=False):
    """Read a boolean from a query string, where everything arrives as text."""
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        return raw
    lowered = str(raw).strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ValidationError({field: "Enter a valid boolean value."})


def _resolve_location(request, entity, raw, field="location"):
    """Resolve a caller-supplied location reference inside this entity and their sites.

    Accepts a primary key or a code, like the other entity-safe resolvers here, and
    refuses one belonging to another entity, or to a site this caller does not work
    in, before any service sees it. Both are reported the same way, so neither can
    be discovered by trying references. ``None`` is returned when nothing was given,
    so the stock service applies its own defaulting.
    """
    if raw in (None, ""):
        return None
    lookup = {"pk": raw} if str(raw).isdigit() else {"code": str(raw)}
    location = _catalogue_visible(
        request, StockLocation.objects.filter(entity=entity),
    ).filter(**lookup).first()
    if location is None:
        raise ValidationError({field: "No such stock location in this entity."})
    return location


def _implied_location(request, entity, field="location"):
    """The store a caller who names none is moving stock in, or ``None`` for theirs.

    ``None`` hands the decision to the stock service's own defaulting, which is what
    a caller entitled to every store gets: a school with one store keeps moving stock
    without naming it, and a school with several asks which, exactly as before.

    A caller pinned to a branch is answered from the stores they work in instead,
    because the service answers from the entity's and cannot tell the difference: a
    school whose only book store stands at one branch would otherwise have a
    storekeeper at another issuing from a shelf they cannot even open by id. One
    visible store needs no naming; none is refused, and so is a choice between
    several, without naming a store the caller may not see.
    """
    if not _branch_scope(request).is_narrowed:
        return None
    visible = list(
        _catalogue_visible(
            request, StockLocation.objects.filter(entity=entity, is_active=True),
        ).order_by("-is_default", "code")[:2]
    )
    if not visible:
        raise ValidationError(
            {field: "You have no stock location here. Ask for one to be set up."})
    if len(visible) > 1:
        raise ValidationError(
            {field: "You work in more than one stock location, so say which."})
    return visible[0]


def _movement_location(request, entity, raw, field="location"):
    """The store a movement applies to, named by the caller or implied by their own.

    The write-side counterpart of :func:`_readable_balances`: a person may only move
    stock where they may read it, whether they say where or leave it to be worked
    out.
    """
    return (
        _resolve_location(request, entity, raw, field)
        or _implied_location(request, entity, field)
    )


class StockLocationListCreateView(_ProcBase):
    """GET (list) / POST (create) the places stock physically sits.

    A location may name a branch or none. An entity-wide store leaves it blank; a
    two-branch school gives each branch its own, and a branch with a main store and a
    lab store gives each of those one. Exactly one location per entity is the default,
    which is what a single-store school relies on to keep moving stock without naming
    one.

    docstring-name: Stock locations
    """

    @property
    def rbac_permission(self):
        return ("procurement.stock.view" if self.request.method == "GET"
                else "procurement.stock.create")

    def get(self, request):
        """List this entity's locations, newest default first."""
        entity = resolve_entity(request)
        qs = _catalogue_visible(
            request, StockLocation.objects.filter(entity=entity),
        ).select_related("branch")
        if (active := request.query_params.get("is_active")) not in (None, ""):
            qs = qs.filter(is_active=_flag(active, "is_active"))
        return self.paginate(
            request, qs.order_by("-is_default", "code"), StockLocationSerializer)

    @transaction.atomic
    def post(self, request):
        """Create a location, keeping exactly one default in the entity."""
        entity = resolve_entity(request)
        body = request.data or {}
        serializer = StockLocationSerializer(data=body)
        serializer.is_valid(raise_exception=True)
        # ``shared_when_ambiguous=True``: a central store belongs to the whole
        # school and is the ordinary shape for a school with one, so a caller
        # covering several sites who names none is filing one of those rather
        # than being asked which site it sits at. Naming a site they do not work
        # in is refused rather than quietly retargeted.
        branch = _raised_branch(request, entity, body, shared_when_ambiguous=True)

        # The first location an entity has must be its default, otherwise nothing
        # resolves for a caller that names none and the entity cannot move stock.
        has_any = StockLocation.objects.filter(entity=entity).exists()
        wants_default = _strict_bool(body.get("is_default", not has_any), "is_default")
        if wants_default:
            StockLocation.objects.filter(entity=entity, is_default=True).update(
                is_default=False)
        try:
            location = StockLocation.objects.create(
                entity=entity, branch=branch,
                is_default=wants_default or not has_any,
                **{k: serializer.validated_data.get(k, "")
                   for k in ("code", "name", "description")},
                is_active=_strict_bool(body.get("is_active", True), "is_active"),
            )
        except IntegrityError:
            raise ValidationError(
                {"code": "A stock location with this code already exists here."})
        return success_response(
            "Stock location created.",
            data=StockLocationSerializer(location).data, status=201,
        )


class StockLocationDetailView(_ProcBase):
    """GET / PATCH one stock location.

    docstring-name: Stock location detail
    """

    @property
    def rbac_permission(self):
        return ("procurement.stock.view" if self.request.method == "GET"
                else "procurement.stock.update")

    def _location(self, request, pk):
        entity = resolve_entity(request)
        return entity, _catalogue_or_404(
            request, StockLocation.objects.filter(entity=entity), pk,
            "No such stock location in this entity.",
        )

    def get(self, request, pk):
        _entity, location = self._location(request, pk)
        return success_response(
            "Stock location retrieved.", data=StockLocationSerializer(location).data)

    @transaction.atomic
    def patch(self, request, pk):
        """Edit a location. Deactivating one that still holds stock is refused."""
        entity, location = self._location(request, pk)
        body = request.data or {}
        for field, limit in (("name", 200), ("description", 255)):
            if field in body:
                setattr(location, field, _text(body[field], field, limit))
        if "branch" in body:
            location.branch = _raised_branch(
                request, entity, body, shared_when_ambiguous=True,
            )
        if "is_default" in body and _strict_bool(body["is_default"], "is_default"):
            StockLocation.objects.filter(entity=entity, is_default=True).exclude(
                pk=location.pk).update(is_default=False)
            location.is_default = True
        if "is_active" in body:
            active = _strict_bool(body["is_active"], "is_active")
            if not active:
                # Stock cannot be issued from an inactive location, so deactivating one
                # that still holds any would strand it where nobody can reach it.
                held = StockBalance.objects.filter(location=location).exclude(
                    on_hand_qty=0, stock_value=0).exists()
                if held:
                    raise ValidationError({"is_active":
                        "This location still holds stock. Move it out first."})
                if location.is_default:
                    raise ValidationError({"is_active":
                        "The default location cannot be deactivated. Make another "
                        "location the default first."})
            location.is_active = active
        location.save()
        return success_response(
            "Stock location updated.", data=StockLocationSerializer(location).data)


class StockBalanceListView(_ProcBase):
    """GET - what each item holds at each location.

    The rows that add up to the item totals every other stock screen shows.

    A caller pinned to a branch reads their own stores and the school's shared
    ones. Naming no store asks about the stores they work in, never about every
    store the entity holds.

    docstring-name: Stock balances by location
    """

    rbac_permission = "procurement.stock.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _readable_balances(request, entity).select_related(
            "stock_item", "location")
        if (item_ref := request.query_params.get("stock_item")):
            qs = qs.filter(stock_item_id=item_ref) if str(item_ref).isdigit() \
                else qs.filter(stock_item__code=item_ref)
        if (loc := _resolve_location(request, entity, request.query_params.get("location"))):
            qs = qs.filter(location=loc)
        if _flag(request.query_params.get("held_only"), "held_only"):
            qs = qs.exclude(on_hand_qty=0, stock_value=0)
        return self.paginate(
            request, qs.order_by("stock_item__code", "location__code"),
            StockBalanceSerializer)


class StockItemListCreateView(_ProcBase):
    """GET (list) / POST (create) stock items - perpetual-inventory masters.

    docstring-name: Stock items
    """

    @property
    def rbac_permission(self):
        """Require stock management for creation and stock visibility for reads."""
        return "procurement.stock.create" if self.request.method == "POST" \
            else "procurement.stock.view"

    def get(self, request):
        """List inventory masters for users with explicit stock visibility.

        ``?location=`` narrows the list to one store, and the rows then report **that
        store's** quantity, value, unit cost and reorder state rather than the entity
        roll-up. Reporting the roll-up under a store filter is the contradiction this
        avoids: an item listed because one branch is short of it, showing a healthy
        total held at another.

        ``?needs_reorder=true`` is measured against whichever scope is in force, so a
        store filter answers "what is this branch short of" rather than "what is the
        school short of, that this branch happens to stock".

        A caller pinned to a branch gets that same treatment without naming a store:
        the figures describe the stores they work in. The catalogue itself is not
        narrowed - an item nothing of theirs holds is listed at nothing, rather than
        hidden from the people who stock it.
        """
        entity = resolve_entity(request)
        location = _resolve_location(request, entity, request.query_params.get("location"))
        qs = StockItem.objects.filter(entity=entity).select_related(
            "inventory_account", "default_expense_account", "catalog_item")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (search := request.query_params.get("q")):
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))

        held = _held_balances(request, entity, location)
        # A named store also narrows which items are listed; a caller's own stores
        # narrow only the figures, because the catalogue is theirs to work from.
        if location is not None:
            qs = qs.filter(pk__in=held.values("stock_item_id"))
        if request.query_params.get("needs_reorder") == "true":
            if held is not None:
                qs = qs.filter(is_active=True, pk__in=_below_reorder(held))
            else:
                qs = qs.filter(is_active=True, on_hand_qty__lte=F("reorder_level"))

        # One query for the page's balances rather than one per row; the serializer
        # overlays them so every derived figure describes the same stores.
        def page_balances(page):
            if held is None:
                return {}
            return {
                "location_balances": _summed_figures(
                    held, [item.pk for item in page]),
            }

        return self.paginate(
            request, qs.order_by("code"), StockItemListSerializer,
            page_context=page_balances,
        )

    def post(self, request):
        """Create a stock master with validated asset/expense accounting defaults."""
        entity = resolve_entity(request)
        body = request.data
        # Codes are entity-unique identifiers - normalise to trimmed upper-case so the
        # uniqueness constraint and later immutability check compare like-for-like.
        code = _text(body.get("code"), "code", 40).upper()
        name = _text(body.get("name"), "name", 200, required=True)
        # Inventory account must be an active, postable ASSET account (required); the
        # default expense account (debited on issue) an active, postable EXPENSE (optional).
        inventory = _resolve_asset_account(
            entity, body.get("inventory_account"), "inventory_account")
        if inventory is None:
            raise ValidationError(
                {"inventory_account": "An inventory asset account is required."})
        try:
            # Keep the uniqueness race inside a savepoint so the request can map the
            # database constraint to a stable field error without a broken transaction.
            with transaction.atomic():
                item = StockItem.objects.create(
                    entity=entity, code=code, name=name,
                    description=_text(body.get("description", ""), "description", 255),
                    unit_of_measure=_text(
                        body.get("unit_of_measure") or "each",
                        "unit_of_measure", 24, required=True,
                    ),
                    catalog_item=_resolve_catalog_item(entity, body.get("catalog_item")),
                    inventory_account=inventory,
                    default_expense_account=_resolve_expense_account(
                        entity, body.get("default_expense_account"), "default_expense_account"),
                    reorder_level=_nonneg_qty(body.get("reorder_level", 0), "reorder_level"),
                    reorder_qty=_nonneg_qty(body.get("reorder_qty", 0), "reorder_qty"),
                    is_active=(
                        _strict_bool(body["is_active"], "is_active")
                        if "is_active" in body else True
                    ),
                )
        except IntegrityError:
            raise ValidationError(
                {"code": "A stock item with this code already exists in this entity."}
            )
        return success_response(
            "Stock item created.",
            data=_detail_payload(request, entity, item.pk),
            status=201,
        )


class StockItemDetailView(_ProcBase):
    """GET (retrieve) / PATCH (update master fields, not balances) one stock item.

    docstring-name: Stock items
    """

    @property
    def rbac_permission(self):
        """Separate stock-master governance from balance/movement visibility."""
        return "procurement.stock.update" if self.request.method == "PATCH" \
            else "procurement.stock.view"

    def get(self, request, pk):
        """Return one item with its newest-first immutable movement ledger."""
        entity = resolve_entity(request)
        return success_response(
            "Stock item retrieved.", data=_detail_payload(request, entity, pk))

    @transaction.atomic
    def patch(self, request, pk):
        """Update master defaults without mutating ledger-owned quantity/value."""
        entity = resolve_entity(request)
        item = StockItem.objects.select_for_update().filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        # The code is the item's stable identifier - sending a different one is an error,
        # never a silent rename (movements/valuation reference it). Compare on the same
        # trimmed upper-case normalisation used at create.
        if "code" in body:
            if _text(body.get("code"), "code", 40).upper() != item.code:
                raise ValidationError({"code": "Stock code cannot be changed."})
        if "name" in body:
            item.name = _text(body.get("name"), "name", 200, required=True)
        if "description" in body:
            item.description = _text(body.get("description", ""), "description", 255)
        if "unit_of_measure" in body:
            item.unit_of_measure = _text(
                body.get("unit_of_measure") or "each",
                "unit_of_measure", 24, required=True,
            )
        if "catalog_item" in body:
            # Preserve an existing inactive historical link, but do not permit a new one.
            item.catalog_item = _resolve_catalog_item(
                entity, body.get("catalog_item"), current_id=item.catalog_item_id,
            )
        if "inventory_account" in body:
            # A changed inventory account must still be an active, postable ASSET account.
            inv = _resolve_asset_account(entity, body.get("inventory_account"), "inventory_account")
            if inv is None:
                raise ValidationError(
                    {"inventory_account": "An inventory asset account is required."})
            if (
                inv.pk != item.inventory_account_id
                and (item.on_hand_qty != 0 or item.stock_value != 0)
            ):
                raise ValidationError({
                    "inventory_account": (
                        "Inventory account cannot be changed while quantity or value "
                        "remains on hand."
                    ),
                })
            item.inventory_account = inv
        if "default_expense_account" in body:
            # Active, postable EXPENSE (or cleared to None).
            item.default_expense_account = _resolve_expense_account(
                entity, body.get("default_expense_account"), "default_expense_account")
        if "reorder_level" in body:
            item.reorder_level = _nonneg_qty(body.get("reorder_level", 0), "reorder_level")
        if "reorder_qty" in body:
            item.reorder_qty = _nonneg_qty(body.get("reorder_qty", 0), "reorder_qty")
        if "is_active" in body:
            item.is_active = _strict_bool(body["is_active"], "is_active")
        # NB: on_hand_qty / stock_value are ledger-owned and deliberately never patchable here.
        item.save()
        return success_response(
            "Stock item updated.",
            data=_detail_payload(request, entity, item.pk))


class StockIssueView(_ProcBase):
    """POST - issue stock out at moving-average cost (Dr expense, Cr inventory).

    docstring-name: Issue stock
    """

    rbac_permission = "procurement.stock.issue"

    def post(self, request, pk):
        """Issue positive quantity at service-computed moving-average cost."""
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        movement = stock.issue_stock(
            item,
            # quantity: strictly positive, finite, bounded (over-issue is caught in the service).
            quantity=_quantity(body.get("quantity"), "quantity"),
            movement_date=_date(body.get("movement_date"), "movement_date")
            or datetime.date.today(),
            # Which store it left. Optional for a caller with one; required once they
            # have more, so nobody has to guess which branch the stock came from.
            location=_movement_location(request, entity, body.get("location")),
            # An override expense account, if given, must be an active postable EXPENSE.
            expense_account=_resolve_expense_account(
                entity, body.get("expense_account"), "expense_account"),
            actor_user=request.user,
            reference=_text(body.get("reference", ""), "reference", 64),
            narration=_text(body.get("narration", ""), "narration", 255),
        )
        return success_response(
            "Stock issued.",
            data={
                "movement": StockMovementSerializer(movement).data,
                "stock_item": _detail_payload(request, entity, item.pk),
            },
            status=201,
        )


class StockAdjustView(_ProcBase):
    """POST - apply a signed stock-count correction (write-up or shrinkage).

    docstring-name: Adjust stock
    """

    rbac_permission = "procurement.stock.adjust"

    def post(self, request, pk):
        """Post a signed count correction through the locked stock service."""
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        unit_cost = body.get("unit_cost")
        movement = stock.adjust_stock(
            item,
            # A signed, non-zero, finite delta (+ write-up, − shrinkage); the service guards
            # a decrease against on-hand and picks the write-up/shrinkage accounts.
            quantity_delta=_signed_qty(body.get("quantity_delta"), "quantity_delta"),
            movement_date=_date(body.get("movement_date"), "movement_date")
            or datetime.date.today(),
            # A count corrects one shelf; say which, unless the caller has only one.
            location=_movement_location(request, entity, body.get("location")),
            # Adjustment account, if given, must be active postable EXPENSE (defaults to 5150).
            adjustment_account=_resolve_expense_account(
                entity, body.get("adjustment_account"), "adjustment_account"),
            # unit_cost only applies to an increase; strict integer kobo when provided.
            unit_cost=_strict_kobo(unit_cost, "unit_cost") if unit_cost not in (None, "") else None,
            actor_user=request.user,
            reference=_text(body.get("reference", ""), "reference", 64),
            narration=_text(body.get("narration", ""), "narration", 255),
        )
        return success_response(
            "Stock adjusted.",
            data={
                "movement": StockMovementSerializer(movement).data,
                "stock_item": _detail_payload(request, entity, item.pk),
            },
            status=201,
        )


class StockItemSummaryView(_ProcBase):
    """GET - stock-item KPI strip (tracked / active / low / out / value).

    ``?location=`` scopes every figure to one store, so the strip cannot disagree
    with the list it sits above. Without that, filtering the list to a branch while
    the KPIs kept reporting the school would put two different answers to the same
    question on one screen. A caller pinned to a branch is scoped the same way
    without naming anything, for the same reason.

    docstring-name: Stock items summary
    """

    rbac_permission = "procurement.stock.view"

    def get(self, request):
        """Return stock counts and carried value in integer kobo, entity or store."""
        entity = resolve_entity(request)
        location = _resolve_location(request, entity, request.query_params.get("location"))
        held = _held_balances(request, entity, location)
        # ONE aggregate every way - conditional counts avoid loading any rows.
        # low_stock: active, at/below its reorder level but still holding something;
        # out_of_stock: active with nothing on hand. total_value sums the carried kobo.
        if location is not None:
            # Counted over that store's balances, and the reorder level still comes
            # from the master because the policy is per item, not per shelf.
            agg = StockBalance.objects.filter(
                location=location, stock_item__entity=entity,
            ).aggregate(
                tracked=Count("id"),
                active=Count("id", filter=Q(stock_item__is_active=True)),
                low_stock=Count("id", filter=Q(
                    stock_item__is_active=True,
                    on_hand_qty__lte=F("stock_item__reorder_level"),
                    on_hand_qty__gt=0)),
                out_of_stock=Count("id", filter=Q(
                    stock_item__is_active=True, on_hand_qty__lte=0)),
                total_value=Sum("stock_value"),
            )
        elif held is not None:
            # Counted over the catalogue and valued over the stores in scope, which
            # is what the list below it does: every item is listed, and the figures
            # on each row describe the stores this caller works in. A school whose
            # every store is in scope therefore reads exactly the totals below.
            agg = StockItem.objects.filter(entity=entity).annotate(
                **_scoped_item_totals(held),
            ).aggregate(
                tracked=Count("id"),
                active=Count("id", filter=Q(is_active=True)),
                low_stock=Count("id", filter=Q(
                    is_active=True, held_qty__lte=F("reorder_level"), held_qty__gt=0)),
                out_of_stock=Count("id", filter=Q(is_active=True, held_qty__lte=0)),
                total_value=Sum("held_value"),
            )
        else:
            agg = StockItem.objects.filter(entity=entity).aggregate(
                tracked=Count("id"),
                active=Count("id", filter=Q(is_active=True)),
                low_stock=Count("id", filter=Q(
                    is_active=True, on_hand_qty__lte=F("reorder_level"), on_hand_qty__gt=0)),
                out_of_stock=Count("id", filter=Q(is_active=True, on_hand_qty__lte=0)),
                total_value=Sum("stock_value"),
            )
        # total_value is a flat kobo integer (+ a formatted naira string), matching the
        # ContractSummary KPI shape the FE strip consumes with formatMoney().
        total_value = agg["total_value"] or 0
        return success_response(
            "Stock summary retrieved.",
            data={
                "tracked": agg["tracked"] or 0,
                "active": agg["active"] or 0,
                "low_stock": agg["low_stock"] or 0,
                "out_of_stock": agg["out_of_stock"] or 0,
                "total_value": total_value,
                "total_value_naira": format_naira(total_value),
            },
        )


class StockMovementListView(_ProcBase):
    """GET - the stock ledger (movements), optionally filtered to one item.

    The ledger covers the stores the caller works in. A storekeeper who cannot
    open another branch's store cannot read what moved through it either, which is
    what leaving the store filter off used to do.

    docstring-name: Stock movements
    """

    rbac_permission = "procurement.stock.view"

    def get(self, request):
        """List this caller's movement ledger with optional item/type filters."""
        entity = resolve_entity(request)
        qs = _readable_movements(request, entity).select_related(
            "stock_item", "created_by", "location")
        if (item_ref := request.query_params.get("stock_item")):
            qs = qs.filter(stock_item_id=item_ref) if str(item_ref).isdigit() \
                else qs.filter(stock_item__code=item_ref)
        if (mtype := request.query_params.get("movement_type")):
            qs = qs.filter(movement_type=mtype)
        if (loc := _resolve_location(request, entity, request.query_params.get("location"))):
            qs = qs.filter(location=loc)
        return self.paginate(request, qs.order_by("-id"), StockMovementSerializer)
