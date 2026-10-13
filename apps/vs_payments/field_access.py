"""Payment gateway fields an administrator may restrict per role.

A collection's origin is set once, when a desk starts it. A refund through the
provider's original payment is named once, when it is tied to the refund; the
rest of it is the provider's word and ours, and nothing can write it.

Otherwise none of these is writable. A virtual account's number and name are issued by
the provider when the account is created. A payout's beneficiary is copied
from the verified vendor record, and a value a caller supplies is only compared
against that record and refused when it differs.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"


def register():
    """Publish the payments field declarations to the Field Access registry."""
    register_fields(
        "payments",
        "collection",
        surfaces=("vs_payments.serializers.CollectionIntentSerializer",),
        fields=(
            FieldSpec("origin", "Collection origin", group="Collection",
                      description="Whether a branch desk started the collection.",
                      writable=True, open_on_create=True, scope=_TENANT, sort_order=10),
        ),
    )
    register_fields(
        "payments",
        "refund",
        surfaces=("vs_payments.serializers.ProviderRefundSerializer",),
        fields=(
            FieldSpec("collection", "Original payment", group="Refund",
                      description="The online payment the refund returns.",
                      writable=True, open_on_create=True, scope=_TENANT, sort_order=10,
                      api_names=("collection_id", "collection_reference")),
            FieldSpec("status", "Refund status", group="Refund",
                      description="Where the refund stands, with the provider's own word for it.",
                      writable=False, scope=_TENANT, sort_order=20,
                      api_names=("status", "provider_status")),
            FieldSpec("provider_reference", "Provider reference", group="Refund",
                      writable=False, scope=_TENANT, sort_order=30),
            FieldSpec("failure_reason", "Failure reason", group="Refund",
                      writable=False, scope=_TENANT, sort_order=40),
        ),
    )
    register_fields(
        "payments",
        "virtual_account",
        surfaces=("vs_payments.serializers.VirtualAccountSerializer",),
        fields=(
            FieldSpec("account_number", "Account number", group="Banking",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=10),
            FieldSpec("account_name", "Account name", group="Banking",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=20),
        ),
    )
    register_fields(
        "payments",
        "payout",
        surfaces=("vs_payments.serializers.PayoutInstructionSerializer",),
        fields=(
            FieldSpec("beneficiary_name", "Beneficiary name", group="Banking",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=10),
            FieldSpec("beneficiary_account_number", "Beneficiary account number",
                      group="Banking", sensitive=True, writable=False,
                      scope=_TENANT, sort_order=20),
            FieldSpec("beneficiary_bank_code", "Beneficiary bank code", group="Banking",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=30),
        ),
    )
