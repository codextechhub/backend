"""Vendor and vendor-contact fields an administrator may restrict per role.

Two resources, because a supplier and the people at the supplier are different
records that happen to share the words email and phone.

``procurement.vendor`` is the supplier's own record. Its contact, tax and
banking details are sensitive, so a role reaches none of them until a school
turns them on: reaching the vendor record is ``procurement.vendor.view``, and
reading the bank account on it is a separate decision, which is what keeps
raising an order and paying for it apart. Its category, payment terms, KYC
status, risk rating and hold are open to every role until a school closes
them. The vendor's name is not a switch: it is the label every picker,
document and message identifies the supplier by.

``procurement.vendor_contact`` is the name, email and phone of each person who
receives quotation requests and orders, and the four routing flags that say how
the person is used. It is a fields-only resource, like the guardians of a
pupil: the contact people are read and corrected with the vendor keys, so
there is no permission of their own. The three details are sensitive so a role
reaches them only when a school opens them; the four flags (primary contact,
receives quotation requests, receives purchase orders, active) are open to
every role until a school closes them, each with its own switch, because
whoever may change a flag decides where a supplier's orders are sent. They are
carried by ``VendorContactSerializer`` wherever a vendor lists its contacts and
by ``RfqInvitationRecipientSerializer`` wherever a quotation request lists who
it was sent to, so one switch governs a person's email on both screens.

The flags also choose the addresses an order or a quotation request goes to, so
a list of those addresses is a statement of the flags that chose it: the order
email preview and the recipients of a quotation request are shown only to a
caller who may read the flags that did the choosing.

A supplier's contact list is replaced as a whole through the vendor create and
update endpoints. Submitting one needs Write on at least one of the seven
fields; adding a contact or removing one needs Write on the email, because the
email is what identifies the contact; and a value the caller cannot write
keeps what the contact already holds (the default, for a new contact).

``procurement.approval`` protects the read-only evidence on a person's
own Procurement decisions. A former branch approver retains this narrow
receipt, but cannot use it to recover the document amount, requester, title,
comments, attachments or full workflow trail. The document reference remains
visible because document numbers are record identifiers, not business fields.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"
_VENDOR_SURFACES = (
    "vs_procurement.serializers.VendorSerializer",
    "vs_procurement.serializers.VendorListSerializer",
)
_CONTACT_SURFACES = (
    "vs_procurement.serializers.VendorContactSerializer",
    "vs_procurement.serializers.RfqInvitationRecipientSerializer",
)
_APPROVAL_HISTORY_SURFACES = (
    "vs_procurement.views.approval_queue.ProcurementApprovalHistoryReceiptSerializer",
)

#: The registry keys of a vendor contact's details, in the order a form shows them.
CONTACT_NAME = "procurement.vendor_contact.name"
CONTACT_EMAIL = "procurement.vendor_contact.email"
CONTACT_PHONE = "procurement.vendor_contact.phone"
CONTACT_KEYS = (CONTACT_NAME, CONTACT_EMAIL, CONTACT_PHONE)

#: The routing flags of a contact, which choose where orders and requests go.
CONTACT_PRIMARY = "procurement.vendor_contact.is_primary"
CONTACT_RECEIVES_RFQS = "procurement.vendor_contact.receives_rfqs"
CONTACT_RECEIVES_ORDERS = "procurement.vendor_contact.receives_purchase_orders"
CONTACT_ACTIVE = "procurement.vendor_contact.is_active"
CONTACT_FLAG_KEYS = (
    CONTACT_PRIMARY, CONTACT_RECEIVES_RFQS, CONTACT_RECEIVES_ORDERS, CONTACT_ACTIVE,
)


def register():
    """Publish Procurement's field declarations to the registry."""
    register_fields(
        "procurement",
        "vendor",
        surfaces=_VENDOR_SURFACES,
        # The vendor form sends the category as ``category`` (an id or a code)
        # and reads it back as ``category_id`` and ``category_code``.
        write_aliases={"category": "category"},
        fields=(
            FieldSpec("email", "Email", group="Contact", sensitive=True,
                      scope=_TENANT, sort_order=10),
            FieldSpec("phone", "Phone", group="Contact", sensitive=True,
                      scope=_TENANT, sort_order=20),
            FieldSpec("address", "Address", group="Contact", sensitive=True,
                      scope=_TENANT, sort_order=30),
            FieldSpec("tax_id", "Tax ID", group="Banking", sensitive=True,
                      scope=_TENANT, sort_order=10),
            FieldSpec("bank_name", "Bank name", group="Banking", sensitive=True,
                      scope=_TENANT, sort_order=20),
            FieldSpec("bank_code", "Bank code", group="Banking", sensitive=True,
                      scope=_TENANT, sort_order=30),
            FieldSpec("bank_account_number", "Bank account number", group="Banking",
                      sensitive=True, scope=_TENANT, sort_order=40),
            FieldSpec("bank_account_name", "Bank account name", group="Banking",
                      sensitive=True, scope=_TENANT, sort_order=50),
            FieldSpec("category", "Category", group="Profile", scope=_TENANT,
                      sort_order=10, api_names=("category_id", "category_code"),
                      description="The purchasing category the vendor is filed under."),
            FieldSpec("payment_terms", "Payment terms", group="Terms", scope=_TENANT,
                      sort_order=10,
                      description="How many days the vendor gives the school to pay a bill."),
            FieldSpec("kyc_status", "KYC status", group="Compliance", scope=_TENANT,
                      sort_order=10,
                      description="Whether the vendor's identity checks are pending, verified or rejected."),
            FieldSpec("risk", "Risk rating", group="Compliance", scope=_TENANT,
                      sort_order=20),
            FieldSpec("on_hold", "On hold", group="Compliance", scope=_TENANT,
                      sort_order=30,
                      description="Whether new orders and payments to the vendor are blocked."),
        ),
    )
    register_fields(
        "procurement",
        "vendor_contact",
        surfaces=_CONTACT_SURFACES,
        fields=(
            FieldSpec("name", "Contact person's name", group="Contact people",
                      sensitive=True, scope=_TENANT, sort_order=10,
                      description="The person who receives quotation requests and orders."),
            FieldSpec("email", "Contact person's email", group="Contact people",
                      sensitive=True, scope=_TENANT, sort_order=20,
                      description="Where quotation requests and orders are sent."),
            FieldSpec("phone", "Contact person's phone", group="Contact people",
                      sensitive=True, scope=_TENANT, sort_order=30),
            FieldSpec("is_primary", "Primary contact", group="Routing", scope=_TENANT,
                      sort_order=10,
                      description="The contact an order goes to when nobody is set to receive orders."),
            FieldSpec("receives_rfqs", "Receives quotation requests", group="Routing",
                      scope=_TENANT, sort_order=20,
                      description="Whether the person is sent the vendor's quotation requests."),
            FieldSpec("receives_purchase_orders", "Receives purchase orders",
                      group="Routing", scope=_TENANT, sort_order=30,
                      description="Whether the person is sent the vendor's purchase orders."),
            FieldSpec("is_active", "Active contact", group="Routing", scope=_TENANT,
                      sort_order=40,
                      description="An inactive contact is sent nothing and cannot open a quotation request."),
        ),
    )
    register_fields(
        "procurement",
        "approval",
        surfaces=_APPROVAL_HISTORY_SURFACES,
        fields=(
            FieldSpec("title", "Document title", group="Decision receipt",
                      scope=_TENANT, sort_order=10, writable=False),
            FieldSpec("decision", "Decision", group="Decision receipt",
                      scope=_TENANT, sort_order=20, writable=False),
            FieldSpec("stage", "Approval stage", group="Decision receipt",
                      scope=_TENANT, sort_order=30, writable=False),
            FieldSpec("decided_at", "Decision time", group="Decision receipt",
                      scope=_TENANT, sort_order=40, writable=False),
            FieldSpec("on_behalf_of", "Acted on behalf of", group="Decision receipt",
                      scope=_TENANT, sort_order=50, writable=False),
            FieldSpec("reversed_at", "Reversal time", group="Decision receipt",
                      scope=_TENANT, sort_order=60, writable=False),
        ),
    )
