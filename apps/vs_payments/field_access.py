"""Payment gateway fields an administrator may restrict per role.

None of these is writable. A virtual account's number and name are issued by
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
