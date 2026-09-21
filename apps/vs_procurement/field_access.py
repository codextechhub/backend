"""Vendor fields an administrator may restrict per role.

A supplier's contact and banking details. ``VendorSerializer`` withholds each
from a role whose Read switch is off, and ``views/vendors.py`` refuses to write
one whose Write switch is off. Every one is written by the vendor create and
update endpoints, ``contacts`` through ``_replace_vendor_contacts``.

All of them are declared sensitive, so a role reaches none of them until a
school turns it on. Reaching the vendor record is ``procurement.vendor.view``;
reading the bank account on it is a separate decision, which is what keeps
raising an order and paying for it apart.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"
_VENDOR_SURFACES = ("vs_procurement.serializers.VendorSerializer",)


def register():
    """Publish the vendor field declarations to the Field Access registry."""
    register_fields(
        "procurement",
        "vendor",
        surfaces=_VENDOR_SURFACES,
        fields=(
            FieldSpec("email", "Email", group="Contact", sensitive=True,
                      scope=_TENANT, sort_order=10),
            FieldSpec("phone", "Phone", group="Contact", sensitive=True,
                      scope=_TENANT, sort_order=20),
            FieldSpec("address", "Address", group="Contact", sensitive=True,
                      scope=_TENANT, sort_order=30),
            FieldSpec("contacts", "Contact people", group="Contact", sensitive=True,
                      scope=_TENANT, sort_order=40,
                      description="The people who receive quotation requests and orders."),
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
        ),
    )
