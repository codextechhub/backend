"""Vendor category governance, vendor master data, and sensitive-field policy.

Codes and historical category links are stable business identifiers.  Updates
may deactivate master data but do not rewrite snapshots already embedded in
purchase documents.

The create and update routes apply the body themselves rather than through a
serializer, so each one asks Field Access whether the caller may write the
names it carries. Contact, tax, banking, category, terms and compliance values
are refused with 403 for a role whose switches do not reach them, on the same
terms the vendor serializer applies when it hides them on the way out. Creating
a vendor is a create path, so a field declared open on create is allowed there
and governed by its switch only afterwards. The compliance values (KYC status,
risk, hold) additionally need ``procurement.vendor.verify``: the switch says
which roles may see and change the value, the key says who may take the
decision.

A vendor's contact people are a second resource, ``procurement.vendor_contact``.
Replacing the list needs Write on at least one of a contact's name, email,
phone and four routing flags (primary, receives quotation requests, receives
purchase orders, active); adding or removing a contact needs Write on the
email, which identifies the person; and a value the caller cannot write keeps
what the contact already holds, so a clerk who maintains names and emails never
wipes the phone numbers they cannot see or re-routes the orders they cannot
read the routing of.
"""
from __future__ import annotations

import datetime
import re

from django.db import IntegrityError, transaction
from django.core.validators import validate_email
from django.db.models import Count, F, Q, Sum
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity
from vs_finance.constants import AccountType, DocumentStatus
from vs_rbac.field_enforcement import (
    FieldWriteDenied,
    assert_writable,
    can_read,
)
from vs_rbac.field_registry import get_declaration
from vs_rbac.permissions import is_vision_super_admin, user_has_rbac_permission
from vs_rbac.scoping import WholeTenantWriteMixin, transaction_branch_q
from vs_config.clock import tenant_today

from ..constants import PAYMENT_TERM_DAYS, PaymentTerms, VendorKycStatus, VendorRisk
from ..models import (
    Vendor,
    VendorContact,
    VendorCategory,
    VendorInvoice,
)
from ..vendor_field_access import require_vendor_field
from ..serializers import (
    VendorCategorySerializer,
    VendorListSerializer,
    VendorSerializer,
)


from .base import (
    _branch_q,
    _branch_scope,
    _catalogue_or_404,
    _catalogue_visible,
    _ProcBase,
    _resolve_account,
    _resolve_tax,
)

# --------------------------------------------------------------------------- #
# Vendor categories + vendors                                                 #
# --------------------------------------------------------------------------- #

#: The vendor's own registered client names that carry a switch and are written
#: by the create and update routes. Whether a caller may write one is Field
#: Access's answer, not this set's; the set survives as the single written
#: record of which vendor names carry switches, which the registry tests check
#: the declaration against. ``category`` is a body name for the registered
#: ``category_id`` and ``category_code`` (a write alias), and the contact people
#: belong to ``procurement.vendor_contact``.
_SENSITIVE_VENDOR_FIELDS = {
    "email", "phone", "address", "tax_id",
    "bank_name", "bank_code", "bank_account_number", "bank_account_name",
    "payment_terms", "kyc_status", "risk", "on_hold",
}
_COMPLIANCE_VENDOR_FIELDS = {"kyc_status", "risk", "on_hold"}

_CONTACT_RESOURCE = "procurement.vendor_contact"
_CONTACT_DETAILS = ("name", "email", "phone")
_CONTACT_LIMITS = {"name": 160, "phone": 32}

#: The routing flags of a contact, each with the value a new contact holds when
#: its row leaves the flag out.
_CONTACT_FLAGS = {
    "is_primary": False,
    "receives_rfqs": True,
    "receives_purchase_orders": False,
    "is_active": True,
}
_CONTACT_FIELDS = _CONTACT_DETAILS + tuple(_CONTACT_FLAGS)


def _normalise_code(value):
    """Canonicalize a stable vendor/category identifier for comparison."""
    return str(value or "").strip().upper()


def _normalise_tax_id(value):
    """Canonicalize tax ids so punctuation cannot bypass entity uniqueness."""
    # Tax identifiers compare without punctuation/case so formatting cannot bypass uniqueness.
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _clean_text(body, field, max_length, *, upper=False, lower=False):
    """Normalize bounded vendor text before it reaches a model field."""
    value = str(body.get(field) or "").strip()
    value = value.upper() if upper else value.lower() if lower else value
    if len(value) > max_length:
        raise ValidationError({field: f"Ensure this field has no more than {max_length} characters."})
    return value


def _validate_email(value):
    """Validate optional contact email while returning its normalized value."""
    if value:
        try:
            validate_email(value)
        except Exception as exc:
            raise ValidationError({"email": "Enter a valid email address."}) from exc
    return value


def _contact_label(detail):
    """The Field Access label of a contact detail, for a refusal read by a person."""
    declaration = get_declaration("procurement", "vendor_contact")
    return next(spec.label for spec in declaration.fields if spec.name == detail)


def _writable_contact_details(request):
    """The contact fields (details and routing flags) the caller may change, as a set of names.

    A request of ``None`` is a write made for nobody, which Field Access
    refuses nothing.
    """
    allowed = set()
    for detail in _CONTACT_FIELDS:
        try:
            assert_writable(request, _CONTACT_RESOURCE, [detail])
        except FieldWriteDenied:
            continue
        allowed.add(detail)
    return allowed


def _clean_contact_detail(detail, value):
    """A submitted contact detail, trimmed and bounded the way it is stored."""
    text = str(value or "").strip()
    if detail == "email":
        return text.lower()
    return text[:_CONTACT_LIMITS[detail]]


def _matching_contact(row, by_id, by_email, used):
    """The existing contact a submitted row stands for, or ``None`` for a new person.

    A row names its contact by ``id`` or, failing that, by email. An id that no
    longer belongs to the vendor is ignored rather than refused, because a form
    that saved twice without reloading carries ids the first save replaced.
    """
    candidate = None
    raw_id = row.get("id")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        candidate = by_id.get(raw_id)
    elif isinstance(raw_id, str) and raw_id.isdigit():
        candidate = by_id.get(int(raw_id))
    if candidate is None and "email" in row:
        candidate = by_email.get(str(row.get("email") or "").strip().lower())
    if candidate is not None and candidate.pk in used:
        return None
    return candidate


def _replace_vendor_contacts(vendor, raw, *, request=None):
    """Validate and replace a vendor's quotation contacts.

    Field Access decides what the caller may put in the list. Submitting one
    at all needs Write on a name, an email, a phone or a routing flag; adding
    or removing a contact needs Write on the email; and each value the caller
    may not write keeps what the matching contact holds (the default for a new
    one), so the list a restricted form posts back never erases or re-routes
    what it could not show. A routing flag a row leaves out keeps the stored
    value for the same reason, and a new contact's flags start at the model's
    defaults, except that the first contact of a vendor with none is primary.
    """
    if not isinstance(raw, list):
        raise ValidationError({"contacts": "Expected a list of vendor contacts."})
    if len(raw) > 20:
        raise ValidationError({"contacts": "A vendor may have up to 20 contacts."})
    allowed = _writable_contact_details(request)
    if not allowed:
        labels = {field: _contact_label(field) for field in _CONTACT_FIELDS}
        raise FieldWriteDenied(list(_CONTACT_FIELDS), labels=labels)
    existing = list(vendor.contacts.all())
    by_id = {contact.pk: contact for contact in existing}
    by_email = {contact.email.lower(): contact for contact in existing}
    used = set()
    cleaned = []
    seen = set()
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValidationError({"contacts": "Every contact must be an object."})
        match = _matching_contact(row, by_id, by_email, used)
        if match is not None:
            used.add(match.pk)
        submitted = {
            detail: _clean_contact_detail(detail, row[detail])
            for detail in _CONTACT_DETAILS if detail in row
        }
        submitted.update({flag: bool(row[flag]) for flag in _CONTACT_FLAGS if flag in row})
        if match is not None:
            stored = {"name": match.name, "email": match.email.lower(), "phone": match.phone}
            stored.update({flag: getattr(match, flag) for flag in _CONTACT_FLAGS})
        else:
            stored = {"name": "", "phone": "", **_CONTACT_FLAGS}
            stored["is_primary"] = not existing and index == 0
        assert_writable(
            request, _CONTACT_RESOURCE, submitted, creating=match is None,
            current={field: stored[field] for field in submitted if field in stored},
        )
        values = {}
        for field in _CONTACT_FIELDS:
            if field in allowed and field in submitted:
                values[field] = submitted[field]
            elif field in _CONTACT_FLAGS or field not in allowed:
                values[field] = stored.get(field, "")
            else:
                values[field] = ""
        email = _validate_email(values["email"])
        if not email or email in seen:
            raise ValidationError({"contacts": "Every contact needs a unique email address."})
        seen.add(email)
        cleaned.append({**values, "email": email})
    if "email" not in allowed and any(contact.pk not in used for contact in existing):
        raise FieldWriteDenied(["email"], labels={"email": _contact_label("email")})
    if cleaned and not any(row["is_primary"] for row in cleaned):
        cleaned[0]["is_primary"] = True
    first_primary = next((i for i, row in enumerate(cleaned) if row["is_primary"]), None)
    for index, row in enumerate(cleaned):
        row["is_primary"] = index == first_primary
    vendor.contacts.all().delete()
    VendorContact.objects.bulk_create([VendorContact(vendor=vendor, **row) for row in cleaned])


def _may_write_contact_email(request):
    """Whether the caller may change a contact's email, which the vendor's own email mirrors."""
    return "email" in _writable_contact_details(request)


def _has_vendor_verification_access(request):
    """Check compliance-governance access in the request's tenant/branch context."""
    if is_vision_super_admin(request.user):
        return True
    tenant = getattr(request, "rbac_tenant", None) or getattr(request, "tenant", None)
    return user_has_rbac_permission(
        request.user, "procurement.vendor.verify",
        tenant=tenant or getattr(request.user, "tenant", None),
    )


def _require_vendor_verification_access(request, body):
    """Require the compliance permission only for KYC, risk, and hold changes."""
    if _COMPLIANCE_VENDOR_FIELDS.intersection(body) and not _has_vendor_verification_access(request):
        raise PermissionDenied("You do not have permission to modify vendor compliance fields.")


def _resolve_category(entity, ref):
    """Resolve an optional category inside the selected ledger entity."""
    if ref in (None, ""):
        return None
    qs = VendorCategory.objects.filter(entity=entity)
    category = qs.filter(pk=ref).first() if str(ref).isdigit() else qs.filter(code__iexact=str(ref)).first()
    if category is None:
        raise ValidationError({"category": "No such vendor category in this entity."})
    return category


def _resolve_assignable_category(entity, ref, *, current_id=None):
    """Resolve a category for a new assignment without hiding legacy inactive links."""
    category = _resolve_category(entity, ref)
    if category is not None and not category.is_active and category.pk != current_id:
        raise ValidationError({"category": "Select an active vendor category."})
    return category


def _validate_account_type(account, field, allowed):
    """Require a usable postable account of an allowed accounting type."""
    if account is not None and account.account_type not in allowed:
        labels = ", ".join(sorted(allowed))
        raise ValidationError({field: f"Select an active {labels} account in this entity."})
    if account is not None and (not account.is_active or not account.is_postable):
        raise ValidationError({field: "Select an active, postable account."})
    return account


def _validate_choice(value, choices, field):
    """Require an exact model choice without permissive coercion."""
    if value not in choices:
        raise ValidationError({field: "Select a valid value."})
    return value


def _validate_bool(value, field):
    """Reject string/number lookalikes for governance booleans."""
    if not isinstance(value, bool):
        raise ValidationError({field: "Enter a valid boolean value."})
    return value


def _duplicate_error(exc):
    """Map vendor code/tax-id uniqueness races to stable field errors."""
    text = str(exc)
    if "tax_id" in text:
        return ValidationError({"tax_id": "A vendor with this tax identifier already exists in this entity."})
    return ValidationError({"code": "A vendor with this code already exists in this entity."})


def _category_duplicate_error(exc):
    """Map the category entity/code constraint to a public validation error."""
    return ValidationError({"code": "A category with this code already exists in this entity."})


def _category_text(body, field, max_length, *, upper=False):
    """Normalize and bound category text independently of vendor payloads."""
    value = str(body.get(field) or "").strip()
    value = value.upper() if upper else value
    if len(value) > max_length:
        raise ValidationError({field: f"Ensure this field has no more than {max_length} characters."})
    return value


def _category_account(request, entity, ref):
    """Resolve an optional active, postable expense default for a category."""
    account = _resolve_account(request, entity, ref, "default_expense_account")
    return _validate_account_type(account, "default_expense_account", {AccountType.EXPENSE})


def _category_depth(category):
    """Return the persisted ancestry depth while rejecting corrupt/cyclic legacy rows."""
    depth = 1
    seen = {category.pk}
    node = category
    while node.parent_id is not None:
        node = node.parent
        if node.pk in seen:
            raise ValidationError({"parent": "Category hierarchy contains a cycle."})
        seen.add(node.pk)
        depth += 1
        if depth > 3:
            raise ValidationError({"parent": "Categories support at most three levels."})
    return depth


def _max_descendant_distance(category):
    """Find subtree height so re-parenting cannot push an existing descendant past level 3."""
    frontier = [category.pk]
    distance = 0
    while frontier:
        frontier = list(VendorCategory.objects.filter(parent_id__in=frontier).values_list("pk", flat=True))
        if frontier:
            distance += 1
        if distance > 2:
            # Existing data should never reach here, but fail closed if a non-API write corrupted it.
            raise ValidationError({"parent": "Categories support at most three levels."})
    return distance


def _category_parent(entity, ref, *, category=None, active=True):
    """Resolve a parent and prove the resulting tree is acyclic and at most 3 deep.

    For a re-parent, both the prospective ancestry and the existing subtree
    height matter; validating only the new parent would allow deep descendants.
    """
    if ref in (None, ""):
        parent = None
    else:
        qs = VendorCategory.objects.filter(entity=entity).select_related(
            "parent", "parent__parent", "parent__parent__parent",
        )
        parent = qs.filter(pk=ref).first() if str(ref).isdigit() else qs.filter(code__iexact=str(ref)).first()
        if parent is None:
            raise ValidationError({"parent": "No such parent category in this entity."})
    if parent is None:
        new_level = 1
    else:
        if active and not parent.is_active:
            raise ValidationError({"parent": "An active category requires an active parent."})
        node = parent
        seen = set()
        while node is not None:
            if node.pk in seen or (category is not None and node.pk == category.pk):
                raise ValidationError({"parent": "A category cannot be its own ancestor."})
            seen.add(node.pk)
            node = node.parent if node.parent_id is not None else None
        new_level = _category_depth(parent) + 1
        if new_level > 3:
            raise ValidationError({"parent": "Level 3 categories cannot have children."})
    if category is not None and new_level + _max_descendant_distance(category) > 3:
        raise ValidationError({"parent": "Re-parenting would move a descendant below level 3."})
    return parent

class _VendorCategoryWriteMixin(WholeTenantWriteMixin):
    """Every write to a vendor category needs whole-tenant reach.

    A category carries no branch: it is the taxonomy every branch files its
    vendors and catalog items under, so renaming or re-parenting one from
    Lekki moves Ikeja's vendors with it.
    """

    shared_subject = "the vendor categories"


class VendorCategoryListCreateView(_VendorCategoryWriteMixin, _ProcBase):
    """GET (list) / POST (create) vendor categories for an entity.

    docstring-name: Vendor categories
    """

    @property
    def rbac_permission(self):
        """Require taxonomy create permission for POST and view for GET."""
        return "procurement.category.create" if self.request.method == "POST" \
            else "procurement.category.view"

    def get(self, request):
        """List category nodes with entity-filtered direct-link usage counts."""
        entity = resolve_entity(request)
        qs = VendorCategory.objects.filter(entity=entity).select_related(
            "default_expense_account", "parent", "parent__parent",
        ).annotate(
            # Count only same-entity vendor links even if legacy ORM writes bypassed API validation.
            vendor_count=Count("vendors", filter=Q(vendors__entity=entity), distinct=True),
            child_count=Count("children", filter=Q(children__entity=entity), distinct=True),
            # Catalog assignments are direct-category counts; descendants keep their own totals.
            catalog_item_count=Count(
                "catalog_items", filter=Q(catalog_items__entity=entity), distinct=True,
            ),
        )
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (search := (request.query_params.get("search") or request.query_params.get("q") or "").strip()):
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        return self.paginate(
            request, qs.order_by("code"), VendorCategorySerializer, context={"request": request},
        )

    @transaction.atomic
    def post(self, request):
        """Create one taxonomy node while serializing hierarchy validation."""
        entity = resolve_entity(request)
        body = request.data
        code = _category_text(body, "code", 32, upper=True)
        name = _category_text(body, "name", 160)
        if not code or not name:
            raise ValidationError({"code": "code and name are required."})
        is_active = body.get("is_active", True)
        is_active = _validate_bool(is_active, "is_active")
        # Hierarchy edits lock the small entity taxonomy so concurrent re-parenting cannot create cycles.
        list(VendorCategory.objects.select_for_update().filter(entity=entity).values_list("pk", flat=True))
        parent = _category_parent(entity, body.get("parent"), active=is_active)
        try:
            # The database constraint is the final race-safe duplicate guard.
            cat = VendorCategory.objects.create(
                entity=entity, code=code, name=name, parent=parent,
                default_expense_account=_category_account(
                    request, entity, body.get("default_expense_account")),
                is_active=is_active,
            )
        except IntegrityError as exc:
            raise _category_duplicate_error(exc) from exc
        return success_response(
            "Vendor category created.", data=VendorCategorySerializer(cat, context={"request": request}).data, status=201,
        )


class VendorCategoryDetailView(_VendorCategoryWriteMixin, _ProcBase):
    """Retrieve or update one category without rewriting linked business documents."""

    @property
    def rbac_permission(self):
        """Separate category governance from taxonomy visibility."""
        return "procurement.category.update" if self.request.method == "PATCH" \
            else "procurement.category.view"

    def _get(self, entity, pk, *, lock=False):
        """Resolve a category with read aggregates or a narrow write lock."""
        qs = VendorCategory.objects.filter(entity=entity, pk=pk)
        if lock:
            # Serialize governance/default changes without locking nullable account joins.
            category = qs.select_for_update(of=("self",)).first()
        else:
            category = qs.select_related(
                "default_expense_account", "parent", "parent__parent",
            ).annotate(
                # Keep the detail aggregate tenant-safe at the join boundary too.
                vendor_count=Count("vendors", filter=Q(vendors__entity=entity), distinct=True),
                child_count=Count("children", filter=Q(children__entity=entity), distinct=True),
                catalog_item_count=Count(
                    "catalog_items", filter=Q(catalog_items__entity=entity), distinct=True,
                ),
            ).first()
        if category is None:
            raise NotFound("No such vendor category in this entity.")
        return category

    def get(self, request, pk):
        """Retrieve one category and its direct, entity-safe usage aggregates."""
        entity = resolve_entity(request)
        category = self._get(entity, pk)
        return success_response("Vendor category retrieved.", data=VendorCategorySerializer(category, context={"request": request}).data)

    @transaction.atomic
    def patch(self, request, pk):
        """Update mutable category governance while preserving its stable code."""
        entity = resolve_entity(request)
        category = self._get(entity, pk, lock=True)
        body = request.data
        # Lock the entity's bounded taxonomy before checking ancestry and subtree height.
        list(VendorCategory.objects.select_for_update().filter(entity=entity).values_list("pk", flat=True))
        if "code" in body and _normalise_code(body.get("code")) != category.code:
            raise ValidationError({"code": "Category code cannot be changed after creation."})
        if "name" in body:
            category.name = _category_text(body, "name", 160)
            if not category.name:
                raise ValidationError({"name": "Category name is required."})
        if "default_expense_account" in body:
            category.default_expense_account = _category_account(
                request, entity, body.get("default_expense_account"),
            )
        desired_active = _validate_bool(body.get("is_active"), "is_active") \
            if "is_active" in body else category.is_active
        if not desired_active and category.children.filter(entity=entity, is_active=True).exists():
            raise ValidationError({"is_active": "Deactivate active child categories first."})
        if "parent" in body:
            category.parent = _category_parent(
                entity, body.get("parent"), category=category, active=desired_active,
            )
        elif desired_active and category.parent_id and not category.parent.is_active:
            raise ValidationError({"is_active": "Activate the parent category first."})
        category.is_active = desired_active
        category.save()
        category.vendor_count = category.vendors.filter(entity=entity).count()
        category.child_count = category.children.filter(entity=entity).count()
        category.catalog_item_count = category.catalog_items.filter(entity=entity).count()
        return success_response(
            "Vendor category updated.", data=VendorCategorySerializer(category, context={"request": request}).data,
        )


class VendorCategoryInsightsView(_ProcBase):
    """Category spend aggregates for the list and detail drawers.

    Spend is the bills the reader can open: a branch-bound reader's figures are
    their own branches' bills, the same population their bill list and the spend
    analysis report add up, and never the school's total.

    The whole answer is spend grouped by a vendor's category, so a reader who
    cannot read the category field is refused: what was left would be a set of
    totals that still says which vendors were filed together.
    """

    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return posted-invoice spend in integer kobo for bounded date windows."""
        require_vendor_field(request, "category", "Category")
        entity = resolve_entity(request)
        today = tenant_today(entity.tenant)
        month_start = today.replace(day=1)
        prior_end = month_start - datetime.timedelta(days=1)
        prior_start = prior_end.replace(day=1)
        year_start = today.replace(month=1, day=1)
        posted = Q(
            vendors__entity=entity,
            vendors__invoices__entity=entity,
            vendors__invoices__status=DocumentStatus.POSTED,
        ) & _branch_q(request, entity, prefix="vendors__invoices__")
        rows = VendorCategory.objects.filter(entity=entity).annotate(
            # Invoice joins stay entity-safe through the category -> vendor FK chain.
            spend_mtd=Sum(
                "vendors__invoices__total",
                filter=posted & Q(vendors__invoices__invoice_date__gte=month_start),
            ),
            spend_prior_month=Sum(
                "vendors__invoices__total",
                filter=posted & Q(
                    vendors__invoices__invoice_date__gte=prior_start,
                    vendors__invoices__invoice_date__lte=prior_end,
                ),
            ),
            spend_ytd=Sum(
                "vendors__invoices__total",
                filter=posted & Q(vendors__invoices__invoice_date__gte=year_start),
            ),
        ).order_by("code")
        return success_response("Vendor category insights retrieved.", data=[{
            "category_id": row.id,
            "spend_mtd": row.spend_mtd or 0,
            "spend_prior_month": row.spend_prior_month or 0,
            "spend_ytd": row.spend_ytd or 0,
        } for row in rows])


class VendorListCreateView(_ProcBase):
    """GET (list) / POST (create) vendors for an entity.

    ``?own=true`` lists only the vendors kept by one of the reader's branches
    (every vendor, for a whole-school reader): the ones a branch clerk may pay
    out to, which the payout pickers read rather than offering a shared vendor
    the payout then refuses.

    docstring-name: Vendors
    """

    @property
    def rbac_permission(self):
        """Require vendor create permission for POST and view for GET."""
        return "procurement.vendor.create" if self.request.method == "POST" \
            else "procurement.vendor.view"

    def get(self, request):
        """List entity vendors; serializer context controls sensitive-field exposure."""
        entity = resolve_entity(request)
        qs = _catalogue_visible(
            request, Vendor.objects.filter(entity=entity),
        ).select_related("category", "branch").annotate(
            # Open commitments: issued POs with an unreceived line, in the reader's branches.
            active_po_count=Count(
                "purchase_orders",
                filter=Q(
                    purchase_orders__status=DocumentStatus.APPROVED,
                    purchase_orders__lines__received_qty__lt=F("purchase_orders__lines__quantity"),
                ) & _branch_q(request, entity, prefix="purchase_orders__"),
                distinct=True,
            ),
        )
        if request.query_params.get("own") == "true":
            qs = qs.filter(transaction_branch_q(request))
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (hold := request.query_params.get("on_hold")) in ("true", "false"):
            require_vendor_field(request, "on_hold", "On hold")
            qs = qs.filter(on_hold=hold == "true")
        if (kyc := request.query_params.get("kyc_status")):
            require_vendor_field(request, "kyc_status", "KYC status")
            qs = qs.filter(kyc_status=kyc)
        if request.query_params.get("purchase_eligible") == "true":
            # Eligibility uses the entity's KYC threshold plus universal active and hold checks.
            qs = qs.filter(is_active=True, on_hold=False).exclude(kyc_status=VendorKycStatus.REJECTED)
            from ..constants import VendorPurchaseKycRequirement
            from ..settings import resolve_procurement_settings
            if (
                resolve_procurement_settings(entity).vendor_purchase_kyc_requirement
                == VendorPurchaseKycRequirement.VERIFIED_ONLY
            ):
                qs = qs.filter(kyc_status=VendorKycStatus.VERIFIED)
        if (search := (request.query_params.get("search") or request.query_params.get("q") or "").strip()):
            match = Q(code__icontains=search) | Q(name__icontains=search)
            # A match on the category name would say which category a vendor is in.
            if can_read(request, "procurement.vendor.category"):
                match |= Q(category__name__icontains=search)
            qs = qs.filter(match)
        return self.paginate(
            request, qs.order_by("code"), VendorListSerializer,
            context={"request": request},
        )

    @transaction.atomic
    def post(self, request):
        """Onboard a pending-KYC vendor without allowing self-approved risk state."""
        entity = resolve_entity(request)
        body = request.data
        code = _clean_text(body, "code", 32, upper=True)
        name = _clean_text(body, "name", 200)
        if not name:
            raise ValidationError({"name": "A vendor name is required."})
        from ..settings import resolve_procurement_settings
        policy = resolve_procurement_settings(entity)
        # Onboarding sets compliance itself, so a form that posts the starting
        # values back is not asking to change them.
        assert_writable(
            request, "procurement.vendor", body, creating=True,
            current={
                "kyc_status": VendorKycStatus.PENDING.value,
                "risk": VendorRisk.LOW.value,
                "on_hold": False,
                "payment_terms": policy.default_payment_terms,
            },
        )
        tax_id = _clean_text(body, "tax_id", 32, upper=True)
        if body.get("payable_account"):
            payable = _validate_account_type(
                _resolve_account(request, entity, body.get("payable_account"), "payable_account"),
                "payable_account", {AccountType.LIABILITY},
            )
        else:
            from vs_finance.account_mappings import resolve_mapped_account
            from vs_finance.constants import AccountMappingKey
            payable = resolve_mapped_account(
                entity, AccountMappingKey.ACCOUNTS_PAYABLE, label="payable account",
            )
        expense = _validate_account_type(
            _resolve_account(request, entity, body.get("default_expense_account"), "default_expense_account"),
            "default_expense_account", {AccountType.EXPENSE},
        )
        payment_terms = _validate_choice(
            body.get("payment_terms") or policy.default_payment_terms,
            PaymentTerms.values, "payment_terms",
        )
        try:
            vendor = Vendor.objects.create(
                entity=entity, code=code, name=name,
                category=_resolve_assignable_category(entity, body.get("category")),
                email=_validate_email(_clean_text(body, "email", 254, lower=True)),
                phone=_clean_text(body, "phone", 32), address=str(body.get("address") or "").strip(), tax_id=tax_id,
                tax_id_normalized=_normalise_tax_id(tax_id),
                bank_name=_clean_text(body, "bank_name", 120),
                bank_code=_clean_text(body, "bank_code", 20, upper=True),
                bank_account_number=re.sub(r"\s+", "", _clean_text(body, "bank_account_number", 32, upper=True)),
                bank_account_name=_clean_text(body, "bank_account_name", 160),
                payable_account=payable, default_expense_account=expense,
                default_wht_tax_code=_resolve_tax(entity, body.get("default_wht_tax_code"), "default_wht_tax_code"),
                payment_terms=payment_terms,
                # Onboarding never self-approves compliance or purchasing blocks.
                kyc_status=VendorKycStatus.PENDING, risk=VendorRisk.LOW, on_hold=False, is_active=True,
            )
        except IntegrityError as exc:
            raise _duplicate_error(exc) from exc
        if "contacts" in body:
            _replace_vendor_contacts(vendor, body["contacts"], request=request)
        elif vendor.email and _may_write_contact_email(request):
            VendorContact.objects.create(
                vendor=vendor, name=vendor.name, email=vendor.email,
                is_primary=True, receives_rfqs=True, receives_purchase_orders=True,
            )
        return success_response(
            "Vendor created.", data=VendorSerializer(vendor, context={"request": request}).data, status=201,
        )


class VendorSummaryView(_ProcBase):
    """Vendor KPIs for the reader; spend remains behind the report permission.

    The counts are over the vendors the reader can see, and the year-to-date spend
    over the bills they can open, so a branch-bound reader's spend is their own
    branches' and not the school's.
    """

    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return vendor counts plus posted YTD spend in integer kobo."""
        entity = resolve_entity(request)
        vendors = _catalogue_visible(request, Vendor.objects.filter(entity=entity))
        year_start = tenant_today(entity.tenant).replace(month=1, day=1)
        spend = VendorInvoice.objects.filter(
            _branch_q(request, entity),
            entity=entity, status=DocumentStatus.POSTED, invoice_date__gte=year_start,
        ).aggregate(total=Sum("total"))["total"] or 0
        data = {
            "active": vendors.filter(is_active=True).count(),
            "inactive": vendors.filter(is_active=False).count(),
            "total_spend_ytd": spend,
        }
        # A count by hold, KYC status or terms says how many vendors have them.
        if can_read(request, "procurement.vendor.on_hold"):
            data["on_hold"] = vendors.filter(on_hold=True).count()
            data["active"] = vendors.filter(is_active=True, on_hold=False).count()
        if can_read(request, "procurement.vendor.kyc_status"):
            data["kyc_pending"] = vendors.filter(kyc_status=VendorKycStatus.PENDING).count()
        if can_read(request, "procurement.vendor.payment_terms"):
            terms = [
                PAYMENT_TERM_DAYS.get(value, 0)
                for value in vendors.values_list("payment_terms", flat=True)
            ]
            data["average_payment_days"] = round(sum(terms) / len(terms)) if terms else None
        return success_response("Vendor summary retrieved.", data=data)


class VendorDetailView(_ProcBase):
    """Retrieve or govern one vendor with field-level sensitive-data controls.

    Existing inactive category/account links remain readable for historical
    fidelity; new assignments must satisfy today's active/postable rules.
    """

    @property
    def rbac_permission(self):
        """Separate vendor governance from ordinary vendor visibility."""
        return "procurement.vendor.update" if self.request.method == "PATCH" else "procurement.vendor.view"

    def _get(self, entity, pk, *, lock=False):
        """Fetch an entity vendor this caller works with, optionally locking its master row."""
        if lock:
            # Lock only the vendor row; nullable account/category joins cannot be locked by PostgreSQL.
            return _catalogue_or_404(
                self.request,
                Vendor.objects.select_for_update(of=("self",)).filter(entity=entity),
                pk, "No such vendor in this entity.",
            )
        return _catalogue_or_404(
            self.request,
            Vendor.objects.select_related(
                "category", "payable_account", "default_expense_account",
                "default_wht_tax_code",
            ).prefetch_related("contacts").filter(entity=entity),
            pk, "No such vendor in this entity.",
        )

    def get(self, request, pk):
        """Serialize one vendor, less the fields the caller's role cannot read."""
        entity = resolve_entity(request)
        vendor = self._get(entity, pk)
        return success_response(
            "Vendor retrieved.", data=VendorSerializer(vendor, context={"request": request}).data,
        )

    @transaction.atomic
    def patch(self, request, pk):
        """Apply validated governance changes while preserving the vendor code."""
        entity = resolve_entity(request)
        body = request.data
        assert_writable(request, "procurement.vendor", body)
        # The route still requires vendor.update; compliance fields add a second,
        # narrower authority and are rejected before the vendor row is mutated.
        _require_vendor_verification_access(request, body)
        vendor = self._get(entity, pk, lock=True)
        previous_email = vendor.email

        if "code" in body and _normalise_code(body.get("code")) != vendor.code:
            raise ValidationError({"code": "Vendor code cannot be changed after creation."})
        if "name" in body:
            vendor.name = _clean_text(body, "name", 200)
            if not vendor.name:
                raise ValidationError({"name": "Vendor name is required."})
        if "category" in body:
            vendor.category = _resolve_assignable_category(
                entity, body.get("category"), current_id=vendor.category_id,
            )
        text_fields = {
            "email": 254, "phone": 32, "bank_name": 120,
            "bank_code": 20, "bank_account_name": 160,
        }
        for field, max_length in text_fields.items():
            if field in body:
                value = _clean_text(
                    body, field, max_length,
                    lower=field == "email", upper=field == "bank_code",
                )
                setattr(vendor, field, _validate_email(value) if field == "email" else value)
        if "address" in body:
            vendor.address = str(body.get("address") or "").strip()
        if "bank_account_number" in body:
            vendor.bank_account_number = re.sub(r"\s+", "", _clean_text(body, "bank_account_number", 32, upper=True))
        if "tax_id" in body:
            vendor.tax_id = _clean_text(body, "tax_id", 32, upper=True)
            vendor.tax_id_normalized = _normalise_tax_id(vendor.tax_id)
        if "payable_account" in body:
            vendor.payable_account = _validate_account_type(
                _resolve_account(request, entity, body.get("payable_account"), "payable_account"),
                "payable_account", {AccountType.LIABILITY},
            )
        if "default_expense_account" in body:
            vendor.default_expense_account = _validate_account_type(
                _resolve_account(request, entity, body.get("default_expense_account"), "default_expense_account"),
                "default_expense_account", {AccountType.EXPENSE},
            )
        if "default_wht_tax_code" in body:
            vendor.default_wht_tax_code = _resolve_tax(entity, body.get("default_wht_tax_code"), "default_wht_tax_code")
        if "payment_terms" in body:
            vendor.payment_terms = _validate_choice(body.get("payment_terms"), PaymentTerms.values, "payment_terms")
        if "kyc_status" in body:
            vendor.kyc_status = _validate_choice(body.get("kyc_status"), VendorKycStatus.values, "kyc_status")
        if "risk" in body:
            vendor.risk = _validate_choice(body.get("risk"), VendorRisk.values, "risk")
        if "on_hold" in body:
            vendor.on_hold = _validate_bool(body.get("on_hold"), "on_hold")
        if "is_active" in body:
            vendor.is_active = _validate_bool(body.get("is_active"), "is_active")
        try:
            vendor.save()
        except IntegrityError as exc:
            raise _duplicate_error(exc) from exc
        if "contacts" in body:
            _replace_vendor_contacts(vendor, body["contacts"], request=request)
        elif vendor.email and _may_write_contact_email(request):
            primary = vendor.contacts.filter(is_primary=True).first()
            if primary and primary.email.lower() == str(previous_email or "").lower():
                primary.email = vendor.email
                primary.name = primary.name or vendor.name
                primary.save(update_fields=["email", "name", "updated_at"])
            elif primary is None:
                VendorContact.objects.get_or_create(
                    vendor=vendor, email=vendor.email,
                    defaults={
                        "name": vendor.name, "is_primary": True, "receives_rfqs": True,
                        "receives_purchase_orders": True,
                    },
                )
        return success_response(
            "Vendor updated.", data=VendorSerializer(vendor, context={"request": request}).data,
        )


class VendorInsightsView(_ProcBase):
    """Authoritative spend and operational performance for one vendor.

    Both figures come from the reports behind the analytics screens, under the same
    branch scope those screens use, so a branch-bound reader's drawer shows what
    their branches bought from and paid the vendor, not the school's total.
    """

    # ``analytics`` rather than ``report``: per-vendor spend and performance is
    # the analytical tail, while the category and list insights beside it are
    # the shallow end. One key could not be banded for both.
    rbac_permission = "procurement.analytics.view"

    def get(self, request, pk):
        """Return report-derived YTD spend, fulfilment, and payment metrics."""
        from ..reports import spend_analysis, vendor_performance

        entity = resolve_entity(request)
        vendor = _catalogue_or_404(
            request, Vendor.objects.filter(entity=entity), pk,
            "No such vendor in this entity.",
        )
        year_start = tenant_today(entity.tenant).replace(month=1, day=1)
        scope = _branch_scope(request, entity)
        # Both reports are limited to this vendor so the drawer does not recompute the entity.
        spend_row = next((
            row for row in spend_analysis(
                entity, start_date=year_start, vendor=vendor, branch_scope=scope,
            ).by_vendor if row.key == vendor.code
        ), None)
        perf_row = next((
            row for row in vendor_performance(
                entity, start_date=year_start, vendor=vendor, branch_scope=scope,
            ).rows if row.vendor_id == vendor.id
        ), None)
        return success_response("Vendor insights retrieved.", data={
            "spend_ytd": spend_row.gross if spend_row else 0,
            "invoice_count": spend_row.invoice_count if spend_row else 0,
            "po_count": perf_row.po_count if perf_row else 0,
            "total_ordered": perf_row.total_ordered if perf_row else 0,
            "receipt_count": perf_row.receipt_count if perf_row else 0,
            "on_time_receipts": perf_row.on_time_receipts if perf_row else 0,
            "late_receipts": perf_row.late_receipts if perf_row else 0,
            "on_time_rate": perf_row.on_time_rate if perf_row else None,
            "payment_count": perf_row.payment_count if perf_row else 0,
            "total_paid": perf_row.total_paid if perf_row else 0,
            "average_payment_days": perf_row.avg_payment_days if perf_row else None,
        })
