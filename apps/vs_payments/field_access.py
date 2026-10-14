"""Payment gateway fields an administrator may restrict per role.

A collection's origin is set once, when a desk starts it. A refund through the
provider's original payment is named once, when it is tied to the refund; the
rest of it is the provider's word and ours, and nothing can write it.

Otherwise none of these is writable. A virtual account's number and name are issued by
the provider when the account is created. A payout's beneficiary is copied
from the verified vendor record, and a value a caller supplies is only compared
against that record and refused when it differs.

A branch's payment terminal and the payments made on it are server-owned too:
the provider issues the terminal's code and payment details, the provider
reports who paid, and a person's only acts on a payment are matching and
unmatching it, which take a customer and nothing else. So every field below is
read-only, and the switches are Read switches.

The numbers a terminal notifies are the exception: a name and a WhatsApp number a
person types in, so both are writable. They are open on create, because adding a
number is creating the entry and the Write switch governs only later changes (an
entry is never edited in place; it is removed and added again). They are sensitive
personal data, read by Finance Admin by default, and removing one needs the Read
switch on the number, so a role that cannot see numbers cannot probe for them.

What a terminal payment says about a person is personal data and sensitive: the
payer's email, the sender's name and bank, the customer it was matched to, and
the customers suggested for it. The suggested customers are one switch of their
own, apart from the matched customer, because a suggestion is a candidate for a
person to choose between and not a statement about who paid. Finance Admin
reads the sensitive fields by default, since matching a payment cannot be done
without them.

A top-up of the platform's provider balance (``payments.platform_topup``) is the
platform's own record, so its fields are ``PLATFORM`` scope and held only by
platform roles. The amount, day, bank account, provider, reference and note are
typed by the person recording it: open on create, with Write switches governing
later corrections of a draft. The status, the journal that booked it and the
person who recorded it are the server's, so they carry Read switches only. All
are open by default, since the key that opens the screen is itself platform-only.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"
_PLATFORM = "PLATFORM"


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
    register_fields(
        "payments",
        "terminal",
        surfaces=("vs_payments.serializers.VirtualTerminalSerializer",),
        fields=(
            FieldSpec("code", "Terminal code", group="Terminal", writable=False,
                      scope=_TENANT, sort_order=10),
            FieldSpec("name", "Terminal name", group="Terminal", writable=False,
                      scope=_TENANT, sort_order=20),
            FieldSpec("status", "Terminal status", group="Terminal", writable=False,
                      scope=_TENANT, sort_order=30, api_names=("status", "attempts")),
            FieldSpec("last_error", "Set-up error", group="Terminal", writable=False,
                      scope=_TENANT, sort_order=40),
            FieldSpec("payment_details", "Payment page and transfer details", group="Terminal",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=50,
                      read_by=("finance_admin",)),
            FieldSpec("routed_to_branch_bank", "Settlement route", group="Terminal",
                      writable=False, scope=_TENANT, sort_order=60),
        ),
    )
    register_fields(
        "payments",
        "terminal_destination",
        surfaces=("vs_payments.serializers.TerminalDestinationSerializer",),
        fields=(
            FieldSpec("name", "Name of the person notified", group="Notification number",
                      sensitive=True, scope=_TENANT, sort_order=10, open_on_create=True,
                      read_by=("finance_admin",)),
            FieldSpec("number", "WhatsApp number", group="Notification number",
                      sensitive=True, scope=_TENANT, sort_order=20, open_on_create=True,
                      read_by=("finance_admin",)),
        ),
    )
    register_fields(
        "payments",
        "terminal_payment",
        surfaces=("vs_payments.serializers.TerminalPaymentSerializer",),
        fields=(
            FieldSpec("amount", "Amount", group="Payment", writable=False, scope=_TENANT,
                      sort_order=10, api_names=("amount", "amount_naira", "fee")),
            FieldSpec("reference", "Payment reference", group="Payment", writable=False,
                      scope=_TENANT, sort_order=20,
                      api_names=("reference", "provider_reference")),
            FieldSpec("terminal", "Terminal", group="Payment", writable=False,
                      scope=_TENANT, sort_order=30, api_names=("terminal", "terminal_name")),
            FieldSpec("paid_at", "Date paid", group="Payment", writable=False,
                      scope=_TENANT, sort_order=40,
                      api_names=("paid_at", "booked_on", "created_at")),
            FieldSpec("status", "Match status", group="Match", writable=False, scope=_TENANT,
                      sort_order=50, api_names=("status", "match_method", "match_note")),
            FieldSpec("matched_by", "Matched by", group="Match", writable=False,
                      scope=_TENANT, sort_order=60,
                      api_names=("matched_by", "matched_by_name", "matched_at",
                                 "unmatched_by_name", "unmatched_at")),
            FieldSpec("payer_email", "Payer's email", group="Payer", sensitive=True,
                      writable=False, scope=_TENANT, sort_order=70,
                      read_by=("finance_admin",)),
            FieldSpec("sender_name", "Sender's name and bank", group="Payer", sensitive=True,
                      writable=False, scope=_TENANT, sort_order=80,
                      api_names=("sender_name", "sender_bank"), read_by=("finance_admin",)),
            FieldSpec("matched_customer", "Customer it was matched to", group="Match",
                      sensitive=True, writable=False, scope=_TENANT, sort_order=90,
                      api_names=("matched_customer", "matched_customer_code",
                                 "matched_customer_name"),
                      read_by=("finance_admin",)),
            FieldSpec("suggestions", "Suggested customers", group="Match", sensitive=True,
                      writable=False, scope=_TENANT, sort_order=100,
                      read_by=("finance_admin",)),
        ),
    )
    register_fields(
        "payments",
        "platform_topup",
        surfaces=("vs_payments.serializers.ProviderBalanceTopUpSerializer",),
        fields=(
            FieldSpec("amount", "Amount", group="Top-up",
                      description="Kobo paid from the platform's bank into the provider balance.",
                      writable=True, open_on_create=True, scope=_PLATFORM, sort_order=10,
                      api_names=("amount", "amount_naira")),
            FieldSpec("topped_up_on", "Date of the top-up", group="Top-up",
                      description="The day the money left the bank, which the top-up books on.",
                      writable=True, open_on_create=True, scope=_PLATFORM, sort_order=20),
            FieldSpec("bank_account", "Bank account paid from", group="Top-up",
                      description="The platform's bank account the money left.",
                      writable=True, open_on_create=True, scope=_PLATFORM, sort_order=30,
                      api_names=("bank_account", "bank_account_name")),
            FieldSpec("provider", "Payment provider", group="Top-up",
                      description="The provider whose balance was topped up.",
                      writable=True, open_on_create=True, scope=_PLATFORM, sort_order=40),
            FieldSpec("reference", "Reference", group="Top-up",
                      description="The provider's or the bank's reference for the transfer.",
                      writable=True, open_on_create=True, scope=_PLATFORM, sort_order=50),
            FieldSpec("note", "Note", group="Top-up",
                      writable=True, open_on_create=True, scope=_PLATFORM, sort_order=60),
            FieldSpec("status", "Status and approval", group="Approval",
                      description="Where the top-up stands and where its approval stands.",
                      writable=False, scope=_PLATFORM, sort_order=70,
                      api_names=("status", "approval_state", "approval_returned")),
            FieldSpec("journal", "Booking", group="Approval",
                      description="The journal that booked the top-up, and when.",
                      writable=False, scope=_PLATFORM, sort_order=80,
                      api_names=("journal_id", "journal_number", "posted_at")),
            FieldSpec("created_by", "Recorded by", group="Approval", writable=False,
                      scope=_PLATFORM, sort_order=90,
                      api_names=("created_by", "created_by_name")),
        ),
    )
