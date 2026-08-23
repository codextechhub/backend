"""Payments datasets published to the Export Centre.

Registered from :meth:`vs_payments.apps.VsPaymentsConfig.ready`. Both are
entity-scoped: a collection or a payout belongs to the set of books it settles into.

Payer name and email are marked sensitive - they are supplied by a member of the
public at checkout, and they are the fields most likely to leave the building by
accident.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_SEARCH,
    FILTER_TEXT,
    KIND_CHOICE,
    KIND_DATETIME,
    KIND_MONEY,
    KIND_TEXT,
    Dataset,
    Field,
    FilterDef,
    choice_labels,
    register,
)


# Build the entity-scoped base queryset for gateway collections.
def _collections(scope):
    from .models import CollectionIntent

    return CollectionIntent.objects.filter(entity=scope.entity)


# Build the entity-scoped base queryset for payout instructions.
def _payouts(scope):
    from .models import PayoutInstruction

    return PayoutInstruction.objects.filter(entity=scope.entity)


_COLLECTION_STATUS = choice_labels("vs_payments.constants.CollectionStatus")
_PAYOUT_STATUS = choice_labels("vs_payments.constants.PayoutStatus")
_PROVIDER = choice_labels("vs_payments.constants.PaymentProvider")


# Register every payments dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="payments.collections",
        module="Payments",
        name="Gateway collections",
        description=(
            "Every collection attempt through a payment provider, with provider "
            "reference, status and the receipt it booked. The dataset to reach for "
            "when reconciling a gateway statement."
        ),
        base=_collections,
        permission="payments.collection.view",
        row_cap=200_000,
        default_columns=("reference", "created_at", "amount", "status", "provider"),
        fields=(
            Field("reference", "Our reference", "Collection", KIND_TEXT, locked=True),
            Field("provider_reference", "Provider reference", "Collection", KIND_TEXT),
            Field("provider", "Provider", "Collection", KIND_CHOICE, choices=_PROVIDER),
            Field("channel", "Channel", "Collection", KIND_TEXT),
            Field("status", "Status", "Collection", KIND_CHOICE, choices=_COLLECTION_STATUS),
            Field("amount", "Amount", "Amounts", KIND_MONEY),
            Field("currency", "Currency", "Amounts", KIND_TEXT, source="currency__code"),
            Field("customer_name", "Customer", "Payer", KIND_TEXT, source="customer__name"),
            Field("invoice_number", "Invoice", "Collection", KIND_TEXT,
                  source="invoice__document_number"),
            Field("receipt_number", "Receipt", "Collection", KIND_TEXT,
                  source="payment__document_number"),
            Field("created_at", "Created", "Record", KIND_DATETIME),
            Field("confirmed_at", "Confirmed", "Record", KIND_DATETIME),
            Field("payer_email", "Payer email", "Payer", KIND_TEXT, sensitive=True,
                  description="Restricted: personal contact data."),
            Field("payer_name", "Payer name", "Payer", KIND_TEXT, sensitive=True,
                  description="Restricted: personal data supplied at checkout."),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_COLLECTION_STATUS),
            FilterDef("provider", "Provider", FILTER_CHOICE, choices=_PROVIDER),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("reference", "Our reference"),
                ("provider_reference", "Provider reference"),
                ("customer__name", "Customer"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("reference", "Reference", FILTER_TEXT),
        ),
    ))

    register(Dataset(
        key="payments.payouts",
        module="Payments",
        name="Payout instructions",
        description=(
            "Money sent out through a provider, one row per instruction, with the batch "
            "it belonged to. Bank details are restricted."
        ),
        base=_payouts,
        permission="payments.payout.view",
        row_cap=200_000,
        default_columns=("reference", "created_at", "amount", "status"),
        fields=(
            Field("reference", "Our reference", "Payout", KIND_TEXT, locked=True),
            Field("provider_reference", "Provider reference", "Payout", KIND_TEXT),
            Field("batch_reference", "Batch", "Payout", KIND_TEXT, source="batch__reference"),
            Field("provider", "Provider", "Payout", KIND_CHOICE, choices=_PROVIDER),
            Field("status", "Status", "Payout", KIND_TEXT),
            Field("amount", "Amount", "Amounts", KIND_MONEY),
            Field("narration", "Narration", "Payout", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
            Field("beneficiary_name", "Beneficiary", "Beneficiary", KIND_TEXT, sensitive=True,
                  description="Restricted: beneficiary banking data."),
            Field("beneficiary_account_number", "Account number", "Beneficiary", KIND_TEXT, sensitive=True,
                  description="Restricted: beneficiary banking data."),
            Field("beneficiary_bank_code", "Bank code", "Beneficiary", KIND_TEXT, sensitive=True,
                  description="Restricted: beneficiary banking data."),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            # A choice rather than free text: the payouts screen filters by status
            # GROUP, and a group is several statuses ("Pending" is PENDING and
            # PROCESSING). Only an "is any of" filter can carry that faithfully.
            FilterDef("status", "Status", FILTER_CHOICE, choices=_PAYOUT_STATUS),
            FilterDef("provider", "Provider", FILTER_CHOICE, choices=_PROVIDER),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("reference", "Our reference"),
                ("provider_reference", "Provider reference"),
                ("batch__reference", "Batch"),
            ), description="Matches any one of these, the way the search box does."),
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the collections list screen's filters into export filters.
def _translate_collections(params):
    from vs_exports.catalogue import Unmapped

    from .constants import COLLECTION_GROUPS

    filters, unmapped = [], []
    # The screen's status pill is a GROUP, not a status: "Settled" is SUCCEEDED,
    # "Failed" is FAILED *and* ABANDONED. Expanding it through the same map the
    # list endpoint uses is what makes the file match the table. Mapping the
    # group to its literal name instead would silently return nothing.
    if (group := params.get("group")) in COLLECTION_GROUPS:
        filters.append({"id": "status", "values": list(COLLECTION_GROUPS[group])})
    elif group:
        unmapped.append(Unmapped(
            "group", group,
            "This status group is not one the export recognises, so the file is not "
            "limited by it.",
        ))
    elif value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    if value := params.get("provider"):
        filters.append({"id": "provider", "values": [value]})
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    if value := params.get("customer"):
        unmapped.append(Unmapped(
            "customer", value,
            "The collections export does not filter by customer yet; the file covers "
            "every payer the other filters allow.",
        ))
    if (value := params.get("succeeded")) is not None:
        unmapped.append(Unmapped(
            "succeeded", value,
            "Use the Status filter in the builder to pick the settled states you want.",
        ))
    return filters, unmapped


# Translate the payouts list screen's filters into export filters.
def _translate_payouts(params):
    from vs_exports.catalogue import Unmapped

    from .constants import PAYOUT_GROUPS

    filters, unmapped = [], []
    if (group := params.get("group")) in PAYOUT_GROUPS:
        filters.append({"id": "status", "values": list(PAYOUT_GROUPS[group])})
    elif group:
        unmapped.append(Unmapped(
            "group", group,
            "This status group is not one the export recognises, so the file is not "
            "limited by it.",
        ))
    elif value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    if value := params.get("provider"):
        filters.append({"id": "provider", "values": [value]})
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    if value := params.get("batch"):
        unmapped.append(Unmapped(
            "batch", value,
            "The payouts export does not filter by batch yet; the file covers every "
            "batch the other filters allow.",
        ))
    return filters, unmapped


# Register the payments screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="payments.collections",
        handles=(
            "group", "status", "provider", "search", "customer", "succeeded",
        ),
        label="Payments - Collections",
        dataset_key="payments.collections",
        translate=_translate_collections,
    ))
    register_screen(ScreenBinding(
        key="payments.payouts",
        handles=(
            "group", "status", "provider", "search", "batch",
        ),
        label="Payments - Payouts",
        dataset_key="payments.payouts",
        translate=_translate_payouts,
    ))
