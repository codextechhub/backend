"""Shared request-parsing helpers and the RBAC-gated base view.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.references import find_by_code_or_id
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import inherited_branch_id as _rbac_inherited_branch_id
from vs_rbac.scoping import raised_branch as _rbac_raised_branch
from vs_rbac.scoping import raised_transaction_branch as _rbac_raised_transaction_branch
from vs_rbac.scoping import caller_may_use_branch, transaction_branch_q
from vs_rbac.scoping import resolve_branch as _resolve_branch
from vs_tenants.references import BRANCH_NOT_FOUND

from ..models import (
    Account,
    BankAccount,
    CostCenter,
    Currency,
    Dimension,
    FiscalYear,
    TaxCode,
)


# --------------------------------------------------------------------------- #
# Branch sub-scope: the write half                                            #
# --------------------------------------------------------------------------- #
#
# Finance rows that carry a branch are of two kinds, and a NULL branch means a
# different thing on each (see :mod:`vs_rbac.scoping`):
#
#   * **shared records** - ``Customer`` and ``FeeStructure``. No branch means
#     every branch, and :func:`_raised_branch` may file one that way;
#   * **transactions** - every :class:`FinanceDocument`, and the ``BankAccount``
#     and ``PettyCashFund`` that hold a branch's money. Each names one real
#     branch. A row that *starts* a chain takes it from the person raising it
#     (:func:`_transaction_branch`), and a row that *continues* one takes it from
#     the row it continues and from nothing else (:func:`_inherited_branch_id`):
#     a credit note is its invoice's, a journal is its source document's.
#
# The rules are the platform's, shared with :mod:`vs_procurement`; these are
# one-line adapters supplying ``entity.tenant``, because finance is
# entity-scoped and the rules are tenant-scoped.


def _raised_branch(request, entity, body, *, field="branch",
                   shared_when_ambiguous=False):
    """:func:`vs_rbac.scoping.raised_branch` for a shared record of this entity.

    Only a customer or a fee structure is raised through this, because only a
    shared record may be filed for every branch. A transaction uses
    :func:`_transaction_branch`.
    """
    return _rbac_raised_branch(
        request, entity.tenant, body, field=field,
        shared_when_ambiguous=shared_when_ambiguous,
    )


def _transaction_branch(request, entity, body, *, field="branch"):
    """:func:`vs_rbac.scoping.raised_transaction_branch` for this entity's tenant.

    Never ``None``: a whole-tenant caller at a tenant with several branches names
    one, and at a tenant with one branch gets it without asking.
    """
    return _rbac_raised_transaction_branch(request, entity.tenant, body, field=field)


def _customer_document_branch_id(request, entity, body, customer):
    """The branch id of a document raised against *customer* with no document before it.

    A customer is a shared record. One filed under a branch (a family at Ikeja)
    gives the document that branch whatever the request says, and a caller who
    cannot work there is refused (403). One the school shares across every branch
    has no branch to give, so the document takes the branch the person raising it
    names or works in (:func:`_transaction_branch`), and is never left without one.
    """
    if customer.branch_id is not None:
        return _inherited_branch_id(request, customer)
    return getattr(_transaction_branch(request, entity, body), "pk", None)


def _inherited_branch_id(request, *sources, field="branch"):
    """:func:`vs_rbac.scoping.inherited_branch_id`, for the transactions a row continues.

    A branch-bound caller may continue only a source of their own branches, and
    sources from two branches are a 400.
    """
    return _rbac_inherited_branch_id(request, *sources, field=field)


# --------------------------------------------------------------------------- #
# Shared resolution + coercion helpers (mirror the procurement conventions)   #
# --------------------------------------------------------------------------- #

# Resolve account reference from request data.
#: Passed as ``document_branch`` by a caller naming an account for no document.
NO_DOCUMENT = object()


def _resolve_account(request, entity, ref, field, *, required=False,
                     document_branch=NO_DOCUMENT, noun=None, verb="Pay it from"):
    """Resolve a GL account by **code** (e.g. "1100") or id within ``entity`` and the caller's reach.

    Codes are all digits, so the JSON type decides which is meant: a number is
    an id and a string a code (:mod:`core.references`).
    Returns ``None`` for a blank ``ref`` unless ``required``. ``request`` is required
    because a ledger account behind another branch's bank account is that bank's
    money: it is refused exactly as an unknown account is (see
    :func:`vs_finance.accounts.accounts_a_caller_may_name`).

    A route naming the account a branch's document pays from or deposits into
    passes ``document_branch`` (a Branch or its id) with its ``noun``. A ledger
    account behind a bank account then obeys the bank rule of
    :func:`_resolve_bank_account`: that branch's own, so naming Lekki's bank
    ledger code on an Ikeja asset is the same 400 as choosing Lekki's bank
    account. ``verb`` words the fix for the receiving side ("Deposit it into").
    A ledger account behind no bank account is unaffected.
    """
    from ..accounts import accounts_a_caller_may_name

    if ref in (None, ""):  # Blank input means no account unless required.
        if required:  # Required account missing.
            raise ValidationError({field: "An account (code or id) is required."})
        return None
    qs = accounts_a_caller_may_name(request, Account.objects.filter(entity=entity))
    if document_branch is not NO_DOCUMENT:
        qs = qs.select_related("bank_account", "bank_account__branch")
    acc = find_by_code_or_id(qs, ref)  # A JSON number is an id, a string a code.
    if acc is None:  # Unknown, another entity's, or another branch's bank ledger.
        raise ValidationError({field: f"No account '{ref}' in this entity."})
    if document_branch is not NO_DOCUMENT:
        require_own_branch_bank(
            getattr(acc, "bank_account", None), document_branch,
            noun=noun, field=field, verb=verb,
        )
    return acc  # Return resolved account.


# Resolve tax code reference from request data.
def _resolve_tax(entity, ref, field="tax_code", *, usage=None):
    """Resolve an entity tax code and, when requested, validate its posting side.

    ``usage="sales"`` requires a usable collected/output account for a positive
    rate; ``usage="purchase"`` requires a recoverable code with a usable
    paid/input account.  Validating at the request boundary keeps an unusable tax
    selection attached to the affected line instead of failing later as a generic
    journal-posting error.
    """
    if ref in (None, ""):  # Tax is optional in most finance line payloads.
        return None
    qs = TaxCode.objects.filter(entity=entity).select_related(
        "collected_account", "paid_account",
    )
    tc = find_by_code_or_id(qs, ref)
    if tc is None:  # Reject missing/cross-entity tax refs.
        raise ValidationError({field: f"No tax code '{ref}' in this entity."})
    if usage is not None and usage not in ("sales", "purchase"):
        raise ValueError(f"Unsupported tax usage '{usage}'.")
    if usage is not None and not tc.is_active:
        raise ValidationError({field: f"Tax code '{tc.code}' is inactive."})
    if usage == "purchase" and tc.rate_bps and not tc.is_recoverable:
        raise ValidationError({
            field: (
                f"Tax code '{tc.code}' is not configured as recoverable input tax "
                "and cannot be used on a purchase line."
            ),
        })
    if usage is not None and tc.rate_bps:
        account = tc.collected_account if usage == "sales" else tc.paid_account
        if account is None:
            direction = (
                "collected (output)" if usage == "sales" else "paid (input)"
            )
            raise ValidationError({
                field: (
                    f"Tax code '{tc.code}' cannot be used on a {usage} line until "
                    f"a {direction} account is configured."
                ),
            })
        if not account.is_active or not account.is_postable:
            raise ValidationError({
                field: (
                    f"Tax code '{tc.code}' uses account '{account.code}', which is "
                    "inactive or not postable."
                ),
            })
    return tc  # Return resolved tax code.


# Resolve cost-center reference from request data.
def _resolve_cost_center(entity, ref, field="cost_center"):
    if ref in (None, ""):  # Cost center is optional.
        return None
    qs = CostCenter.objects.filter(entity=entity)
    cc = find_by_code_or_id(qs, ref)
    if cc is None:  # Reject missing/cross-entity cost center refs.
        raise ValidationError({field: f"No cost centre '{ref}' in this entity."})
    return cc  # Return resolved cost center.


# Normalize a request value into a unique string list.
def _str_list(raw, field):
    """Coerce ``raw`` into a list of non-empty, stripped, de-duplicated strings.

    Used for a :class:`~vs_finance.models.Dimension`'s ``allowed_values``. ``None``
    yields ``[]``; anything that is not a list (or holds blank entries) is rejected.
    Order is preserved so the first occurrence of each value wins.
    """
    if raw in (None, ""):  # Blank means no allowed values.
        return []
    if not isinstance(raw, (list, tuple)):  # Only array-like payloads are accepted.
        raise ValidationError({field: "Expected a list of values."})
    seen, out = set(), []  # Track duplicates while preserving first-seen order.
    for item in raw:  # Normalize each supplied value.
        val = str(item).strip()  # Coerce to stripped string.
        if not val:  # Blank values are invalid.
            raise ValidationError({field: "Values cannot be blank."})
        if val not in seen:  # Keep only first occurrence.
            seen.add(val)  # Remember value.
            out.append(val)  # Preserve order.
    return out  # Return cleaned unique list.


# Validate analytical dimensions map.
def _resolve_dimensions(entity, raw, field="dimensions"):
    """Validate an analytical ``{axis_code: value}`` map for ``entity``.

    Mirrors :func:`_resolve_cost_center`: ``None``/``""``/``{}`` yield an empty map.
    Each key must be a registered, active :class:`~vs_finance.models.Dimension` code,
    and each value must be a non-empty string listed in that axis's ``allowed_values``
    (an axis with no values defined yet accepts none). Returns the cleaned map to
    store verbatim on the journal line's ``dimensions`` JSON.
    """
    if raw in (None, ""):  # Blank dimensions become empty map.
        return {}
    if not isinstance(raw, dict):  # Dimensions must be an object/map.
        raise ValidationError({field: "Expected a map of {axis: value}."})
    if not raw:  # Empty map is valid.
        return {}

    allowed = {  # Active dimension code -> allowed values.
        d.code: set(d.allowed_values or [])  # Store allowed values as a set for membership tests.
        for d in Dimension.objects.filter(entity=entity, is_active=True)
    }
    cleaned = {}  # Cleaned dimensions map for storage.
    for axis, value in raw.items():  # Validate each supplied axis/value.
        axis = str(axis)  # Dimension axis codes are strings.
        if axis not in allowed:  # Axis must exist and be active.
            raise ValidationError(
                {field: f"No active dimension '{axis}' in this entity."})
        val = str(value).strip()  # Normalize value.
        if not val:  # Dimension values cannot be blank.
            raise ValidationError({field: f"Dimension '{axis}' needs a value."})
        if val not in allowed[axis]:  # Value must be preconfigured for that axis.
            permitted = ", ".join(sorted(allowed[axis])) or "(none defined)"  # Human-readable allowed values.
            raise ValidationError(
                {field: f"'{val}' is not an allowed value for '{axis}'. "
                        f"Allowed: {permitted}."})
        cleaned[axis] = val  # Store cleaned axis value.
    return cleaned  # Return storage-ready dimensions map.


# Resolve currency code from request data.
def _resolve_currency(ref, field="currency"):
    if ref in (None, ""):  # Currency is optional in many payloads.
        return None
    cur = Currency.objects.filter(code=str(ref).upper()).first()
    if cur is None:  # Reject unknown currency codes.
        raise ValidationError({field: f"No currency '{ref}'."})
    return cur  # Return resolved currency.


# Resolve bank account by id or name.
def _resolve_bank_account(request, entity, ref, field="bank_account", *, required=True,
                          document_branch, noun):
    """Resolve the bank account a document is paid from or into, by id or name.

    Two rules, in this order, and every money-out route names its account here:

    * **Reach.** The account must be one the caller can see in their bank list:
      their own branches' accounts only. Ikeja's bursar naming Lekki's collection
      account gets the same 404 as a mistyped id, which neither pays out of
      Lekki's money nor confirms the account exists.
    * **The document's own branch.** A document is paid only from an account of
      its own branch (:func:`require_own_branch_bank`). Mrs Okafor covers Ikeja
      and Lekki, so they can see Lekki's account, but an Ikeja refund paid from it
      would leave Ikeja's books owing and Lekki's short; that is a 400 naming the
      branch to pay it from.

    ``document_branch`` (a Branch or its id) and ``noun`` ("refund") are required
    so a new money route cannot forget the second rule.
    """
    ba = _bank_account_in_reach(request, entity, ref, field, required=required)
    require_own_branch_bank(ba, document_branch, noun=noun, field=field)
    return ba


def _bank_account_in_reach(request, entity, ref, field="bank_account", *, required=True):
    """The reach half of :func:`_resolve_bank_account` alone.

    For the one route whose document branch is chosen from the account itself: a
    tax return paid without naming a share pays the account's own branch's share
    (:func:`vs_finance.tax_filing.pay_filing`), so the account and the share
    agree by construction. Every other route uses :func:`_resolve_bank_account`.
    """
    if ref in (None, ""):  # Blank input means missing bank account.
        if required:  # Most payment endpoints require a bank account.
            raise ValidationError({field: "A bank account (id or name) is required."})
        return None
    qs = BankAccount.objects.filter(
        transaction_branch_q(request), entity=entity,
    ).select_related("gl_account", "branch")
    ba = (  # Resolve by id for numeric refs, otherwise by name.
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(name=str(ref)).first()
    )
    if ba is None:  # Unknown, another entity's, or outside the caller's branches.
        raise NotFound(f"No bank account '{ref}' in this entity.")
    return ba


def require_own_branch_bank(bank, document_branch, *, noun, field="bank_account", verb="Pay it from"):
    """Refuse ``bank`` for a document of ``document_branch`` unless it is that branch's own.

    Split out for the routes that learn the document's branch only after the
    account is named (a batch, or a vendor payment whose branch comes from the
    bills it settles). The account's branch must equal the document's, with no
    exception for an account without a branch: Lekki's refund paid from such an
    account would take the money from wherever that account's cash really sits.
    The refusal names the branch and the way out, for example "This refund
    belongs to Lekki Branch. Pay it from a Lekki Branch account."

    The two are compared by :func:`vs_rbac.scoping.same_transaction_branch`: at a
    school with one branch, a document or an account not yet given a branch is
    that branch's; at a school with several, a document not yet given a branch is
    paid only from an account not yet given one either.
    """
    from vs_rbac.scoping import same_transaction_branch
    from vs_tenants.models import Branch

    branch_id = getattr(document_branch, "pk", document_branch)
    if bank is None or bank.branch_id == branch_id:
        return
    if same_transaction_branch(bank.entity.tenant_id, bank.branch_id, branch_id):
        return
    if branch_id is None:
        raise ValidationError({field: (
            f"This {noun} has not been given a branch, so it cannot use "
            f"{bank.branch.name}'s account."
        )})
    branch = (
        document_branch if isinstance(document_branch, Branch)
        else Branch.all_objects.get(pk=branch_id)
    )
    article = "an" if branch.name[:1].upper() in "AEIOU" else "a"
    raise ValidationError({field: (
        f"This {noun} belongs to {branch.name}. "
        f"{verb} {article} {branch.name} account."
    )})


# Resolve fiscal year by label or id.
def _resolve_fiscal_year(entity, ref, field="fiscal_year"):
    """Resolve a fiscal year by its ``year`` label (preferred) or id within ``entity``."""
    if ref in (None, ""):  # Fiscal year is required for these endpoints.
        raise ValidationError({field: "Choose the financial year."})
    qs = FiscalYear.objects.filter(entity=entity)
    fy = qs.filter(year=int(ref)).first() if str(ref).isdigit() else None
    if fy is None and str(ref).isdigit():  # Numeric refs can also be primary keys.
        fy = qs.filter(pk=int(ref)).first()
    if fy is None:  # Reject missing/cross-entity fiscal years.
        raise ValidationError({field: f"No fiscal year '{ref}' in this entity."})
    return fy  # Return resolved fiscal year.


# Parse optional/required ISO date.
def _date(value, field, *, required=False):
    if value in (None, ""):  # Blank date.
        if required:  # Required date missing.
            raise ValidationError({field: "An ISO date (YYYY-MM-DD) is required."})
        return None
    try:  # Parse strict ISO date.
        return datetime.date.fromisoformat(str(value))
    except ValueError:  # Invalid date format.
        raise ValidationError({field: "Expected an ISO date (YYYY-MM-DD)."})


# Parse request value as Decimal.
def _dec(value, field):
    try:  # Decimal constructor can reject invalid strings/types.
        return Decimal(str(value))
    except (InvalidOperation, TypeError):  # Invalid numeric input.
        raise ValidationError({field: "Expected a number."})


# Parse non-negative integer kobo.
def _money(value, field):
    """Coerce to non-negative integer kobo, rejecting floats-as-naira mistakes."""
    try:  # int() rejects non-integer strings and missing values.
        amount = int(value)  # Normalize to integer kobo.
    except (TypeError, ValueError):  # Invalid money input.
        raise ValidationError({field: "Enter the amount as a whole number."})
    if amount < 0:  # Non-negative money parser rejects negative amounts.
        raise ValidationError({field: "Amount cannot be negative."})
    return amount  # Return integer kobo.


# Parse signed integer kobo.
def _signed_money(value, field):
    """Coerce to a *signed* integer kobo (bank lines can be negative outflows)."""
    try:  # Signed amounts still must be integers.
        return int(value)  # Return signed integer kobo.
    except (TypeError, ValueError):  # Invalid signed money input.
        raise ValidationError({field: "Enter the amount as a whole number, negative for money out."})


# Extract and validate required line array.
def _require_lines(body):
    lines = body.get("lines")
    if not lines or not isinstance(lines, list):  # Lines must be a non-empty list.
        raise ValidationError({"lines": "At least one line is required."})
    return lines  # Return validated line list.


# Parse integer request value.
def _int(value, field, *, required=False, minimum=None, maximum=None):
    if value in (None, ""):  # Blank integer input.
        if required:  # Required integer missing.
            raise ValidationError({field: "An integer is required."})
        return None
    try:  # Normalize numeric string/int to int.
        out = int(value)  # Parsed integer.
    except (TypeError, ValueError):  # Invalid integer input.
        raise ValidationError({field: "Expected an integer."})
    if minimum is not None and out < minimum:  # Enforce optional lower bound.
        raise ValidationError({field: f"Must be ≥ {minimum}."})
    if maximum is not None and out > maximum:  # Enforce optional upper bound.
        raise ValidationError({field: f"Must be ≤ {maximum}."})
    return out  # Return parsed integer.


#: Spellings of ``?branch=`` asking for the rows no branch owns yet.
UNASSIGNED_REFS = ("unassigned", "none", "null")


def _filter_by_branch(qs, request, entity, *, field: str = "branch", column: str | None = None,
                      also: str | None = None):
    """Narrow *qs* by a ``?branch=`` parameter, or leave it alone.

    ``field`` names the parameter; ``column`` the relation it filters, without
    its ``_id`` (the parameter's own name when left out). The roster filters on
    ``branch_on``, the branch owning each row today
    (:meth:`~vs_finance.models.EmployeeSalaryQuerySet.with_branch_on`).
    ``also`` names a second relation a row may match the branch through, such
    as the shares of a payment from a payer (``shares__branch``): the payment
    received at Ikeja holding Chidi's share for Lekki is in Lekki's list too.
    A ``column`` across a relation (``lines__branch``, the branch lines of a
    provision run) and ``also`` are matched as a subquery, so a row with two
    lines or shares there is listed once.

    One helper for the roster and the runs list because the parameter has to
    mean the same thing on both. ``?branch=unassigned`` finds the people no
    branch owns - the ones blocking a school's switch to per-branch payroll -
    and on the runs list the central runs raised before it switched. Spelled out
    rather than left blank, because a blank parameter is how a frontend says "no
    filter at all", and the two answers are not the same list.

    A branch the caller may not work in is reported exactly like one that does
    not exist, so the parameter cannot be used to enumerate a school's sites.
    """
    column = column or field
    branch_ref = request.query_params.get(field)
    if not branch_ref:
        return qs
    if str(branch_ref).lower() in UNASSIGNED_REFS:
        return qs.filter(**{f"{column}_id__isnull": True})
    branch = _resolve_branch(entity.tenant, branch_ref, field)
    if branch is None or not caller_may_use_branch(request, branch):
        raise ValidationError({field: BRANCH_NOT_FOUND})
    if also or "__" in column:
        from django.db.models import Q

        match = Q(**{f"{column}_id": branch.pk})
        if also:
            match |= Q(**{f"{also}_id": branch.pk})
        return qs.filter(pk__in=qs.model.objects.filter(match).values("pk"))
    return qs.filter(**{f"{column}_id": branch.pk})


# Parse common truthy/falsey request values.
def _bool(value, default=False):
    if value in (None, ""):  # Blank input uses caller default.
        return default
    if isinstance(value, bool):  # Native bool passes through.
        return value
    return str(value).lower() in ("1", "true", "yes", "on")  # Recognize common truthy strings.


# Shared base class for finance operational endpoints.
class _FinanceBase(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Require active auth and RBAC permission.

    # Paginate a queryset with platform envelope.
    def paginate(self, request, qs, serializer_cls, **ser_kwargs):
        """List response via the platform's XVSPagination envelope ({pagination, data}).

        Fixed page size 25 (override per-request with ?page_size=, capped at 100).

        The request rides in the serializer context unless the caller supplies
        one of its own. A serializer only knows whose response it is building
        from the context, and a list built without it would hand every caller
        the same rows whatever their Field Access says.

        Documents dated in an archived fiscal year are left out unless the caller
        asks with ``?include_archived=true`` (:func:`vs_finance.archive.hide_archived`).
        """
        from core.pagination import XVSPagination

        from ..archive import hide_archived

        qs = hide_archived(qs, request)
        paginator = XVSPagination()  # Instantiate platform paginator.
        paginator.page_size = 25  # Default finance page size.
        page = paginator.paginate_queryset(qs, request, view=self)  # Slice queryset for current request.
        ser_kwargs.setdefault("context", {"request": request})
        return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)  # Serialize and wrap page.
