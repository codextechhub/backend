"""Which depth of which module each permission key belongs to.

This is the price list in the only form the code can read. Every key the
backend can refuse is answered here with one of four verdicts:

    a band          the key belongs to a module, at Core, Plus or Advanced
    PLATFORM        CodeX's own key; never sold, so never banded
    NEVER           a role decision, not a commercial one
    None            core for every school, and deliberately so

Two vocabularies meet here and they are not the same list. A permission module
is a namespace a key is filed under (``school``, ``academics``); a capability
module is a thing a school is sold (``students``, ``calendar``). The join
between them is :data:`CAPABILITY_MODULE`, and it is data rather than a guess:
``academics.session`` is core for every school, while ``academics.timetable``
belongs to the Calendar module a school buys.

Bands are declared per resource because that is the level a person can read and
argue with. Where one action sits apart from its resource it is named in
:data:`ACTION_BANDS`, and every entry there carries the reason.

Absence is a decision too. A key that resolves to None is available to every
school whatever it pays, which is the safe direction: the opposite default
would hide working routes from paying schools the day a new module ships.
"""

CORE, PLUS, ADVANCED = "CORE", "PLUS", "ADVANCED"
PLATFORM, NEVER = "PLATFORM", "NEVER"

#: (permission module, resource) -> the capability module it is sold inside.
#: A pair that is absent is core for every school and carries no capability.
CAPABILITY_MODULE = {
    ("school", "students"): "students",
    ("school", "teachers"): "teachers",
    ("school", "staff_records"): "teachers",
    ("school", "leave"): "teachers",
    ("academics", "calendar"): "calendar",
    ("academics", "timetable"): "calendar",
    ("academics", "exam"): "calendar",
}

#: Whole permission modules that answer to one capability module.
CAPABILITY_MODULE_BY_MODULE = {
    "finance": "finance",
    # Collections, payouts and virtual accounts are the finance product's
    # money-movement half; there is no separate capability for them.
    "payments": "finance",
    "procurement": "procurement",
}

#: Resources sold under a band that carries its own name rather than the
#: generic ``<module>_<depth>`` one.
#:
#: Empty, and worth keeping empty. ``vendors`` used to be here and was folded
#: into ``procurement_core``: two capability rows at the same depth of the same
#: module can never be told apart, because a school reaching one always reaches
#: the other. A distinction the code cannot act on eventually persuades
#: somebody that Vendors can be sold on its own.
NAMED_BAND = {}

#: Bulk import and data export are one platform-wide feature each, banded once.
#: A bursar who imported students in January is not refused a vendor list in
#: March for reasons nobody could explain.
#:
#: Named per key rather than per action verb. ``config.audit.export`` also ends
#: in ``export`` and is a different thing entirely: reading your own
#: configuration history is not the data-export product.
ACTION_CAPABILITY = {
    ("school", "students", "import"): "bulk_import",
    ("school", "students", "export"): "data_export",
    ("academics", "structure", "import"): "bulk_import",
}

#: Whole permission modules that are one sellable feature, with the band the
#: feature is sold at.
MODULE_BANDS = {
    "import": PLUS,
    "exports": PLUS,
    "communication": CORE,
}

MODULE_NAMED_CAPABILITY = {
    "import": "bulk_import",
    "exports": "data_export",
    "communication": "email_alerts",
}

RESOURCE_BANDS = {
    # ---- school administration -------------------------------------------
    ("school", "dashboard"): CORE,
    ("school", "branches"): CORE,
    ("school", "profile"): CORE,
    ("school", "settings"): CORE,
    ("school", "roles"): CORE,
    ("school", "administrators"): CORE,
    ("school", "fees"): CORE,
    ("school", "students"): CORE,
    ("school", "teachers"): CORE,
    ("school", "leave"): PLUS,
    ("school", "staff_records"): PLUS,
    ("school", "user_overrides"): CORE,
    ("school", "impersonation"): PLATFORM,

    # ---- academic structure ----------------------------------------------
    ("academics", "session"): CORE,
    ("academics", "classes"): CORE,
    ("academics", "subject"): CORE,
    ("academics", "structure"): CORE,
    ("academics", "calendar"): CORE,
    ("academics", "timetable"): PLUS,
    ("academics", "exam"): ADVANCED,

    # ---- finance ----------------------------------------------------------
    ("finance", "entity"): CORE,
    ("finance", "settings"): CORE,
    ("finance", "account"): CORE,
    ("finance", "costcenter"): CORE,
    ("finance", "dimension"): CORE,
    ("finance", "currency"): CORE,
    ("finance", "fxrate"): CORE,
    ("finance", "taxcode"): CORE,
    ("finance", "period"): CORE,
    ("finance", "journal"): CORE,
    ("finance", "directentry"): CORE,
    ("finance", "customer"): CORE,
    ("finance", "invoice"): CORE,
    ("finance", "payment"): CORE,
    ("finance", "report"): CORE,
    ("finance", "feestructure"): PLUS,
    ("finance", "creditnote"): PLUS,
    ("finance", "refund"): PLUS,
    ("finance", "writeoff"): PLUS,
    ("finance", "concession"): PLUS,
    ("finance", "paymentplan"): PLUS,
    ("finance", "dunning"): PLUS,
    ("finance", "bankaccount"): PLUS,
    ("finance", "budget"): PLUS,
    ("finance", "expenseclaim"): PLUS,
    ("finance", "pettycash"): PLUS,
    ("finance", "pettycashvoucher"): PLUS,
    ("finance", "payrollrun"): ADVANCED,
    ("finance", "salary"): ADVANCED,
    ("finance", "tax"): ADVANCED,
    ("finance", "fixedasset"): ADVANCED,
    ("finance", "audit"): ADVANCED,

    # ---- procurement and vendors -----------------------------------------
    ("procurement", "settings"): CORE,
    ("procurement", "requisition"): CORE,
    # Core at any ladder length, override included. How many rounds a document
    # goes through describes a school's own shape rather than a feature it
    # buys, and it is workflow configuration that no key could gate anyway.
    ("procurement", "approval"): CORE,
    ("procurement", "purchase_order"): CORE,
    ("procurement", "goods_receipt"): CORE,
    ("procurement", "vendor"): CORE,
    ("procurement", "category"): CORE,
    ("procurement", "rfq"): PLUS,
    ("procurement", "competition"): PLUS,
    ("procurement", "quotation"): PLUS,
    ("procurement", "contract"): PLUS,
    ("procurement", "stock"): PLUS,
    ("procurement", "catalog_item"): PLUS,
    ("procurement", "vendor_invoice"): PLUS,
    ("procurement", "vendor_payment"): PLUS,
    ("procurement", "report"): PLUS,
    ("procurement", "analytics"): ADVANCED,
    ("procurement", "vendor_assessment"): ADVANCED,

    # ---- payments ---------------------------------------------------------
    ("payments", "collection"): CORE,
    ("payments", "webhook"): CORE,
    ("payments", "virtual_account"): PLUS,
    ("payments", "payout"): PLUS,
    ("payments", "report"): PLUS,
    ("payments", "payout_batch"): ADVANCED,
    ("payments", "unattributed_webhook"): ADVANCED,

    # ---- the pricing controls themselves ---------------------------------
    ("config", "capability"): PLATFORM,
    ("config", "entitlement"): PLATFORM,
    ("config", "override"): PLATFORM,
}

ACTION_BANDS = {
    # Closing, locking and reopening a period is the accounting tail rather
    # than everyday bookkeeping.
    ("finance", "period", "close"): ADVANCED,
    ("finance", "period", "lock"): ADVANCED,
    ("finance", "period", "reopen"): ADVANCED,

    # Sending a customer their whole account position. Emailing one invoice
    # stays Core: a school that cannot send an invoice cannot collect a fee.
    ("finance", "customer", "email_statement"): PLUS,
    ("finance", "invoice", "writeoff"): PLUS,

    # Promoting the whole roll, which no longer rides on the key that moves one
    # child between branches.
    ("school", "students", "promote"): PLUS,

    # A school's own go-live decision belongs to CodeX, and is already enforced
    # by PlatformDecisionAllowed.
    ("onboarding", "go_live", "approve"): PLATFORM,
    ("onboarding", "go_live", "reject"): PLATFORM,
}

#: Keys that must never carry a band, with the reason each stays out. These
#: decide who inside an organisation may look at something. A school must not
#: be able to buy its way into a child's medical record.
NEVER_BAND = {
    ("school", "students", "view_sensitive"):
        "A child's blood group, allergies and medical conditions.",
    ("procurement", "vendor", "view_sensitive"):
        "A supplier's bank details; separation of duties, not a tier.",
    ("payments", "payout", "view_sensitive"):
        "Destination account details on a payout.",
    ("payments", "virtual_account", "view_sensitive"):
        "Account numbers behind a school's collection accounts.",
    ("finance", "bankaccount", "view_sensitive"):
        "The school's own bank account numbers.",
    ("finance", "payrollrun", "view_sensitive"):
        "Individual salary figures.",
    ("exports", "sensitive_field", "export"):
        "Permission to carry restricted fields out of the platform.",
    ("school", "user_overrides", "manage"):
        "Granting one person an exception to their role.",
    ("school", "user_overrides", "view"):
        "Reading those exceptions.",
}

#: Depth to the suffix the capability catalogue seeds its bands under.
_BAND_SUFFIX = {CORE: "core", PLUS: "plus", ADVANCED: "advanced"}


def band_for(module, resource, action):
    """The verdict for one key: a band, PLATFORM, NEVER, or None for core."""
    if (module, resource, action) in NEVER_BAND:
        return NEVER
    if (module, resource, action) in ACTION_BANDS:
        return ACTION_BANDS[(module, resource, action)]
    if module == "platform":
        return PLATFORM
    if (module, resource) in RESOURCE_BANDS:
        return RESOURCE_BANDS[(module, resource)]
    return MODULE_BANDS.get(module)


def capability_key_for(module, resource, action):
    """The capability key that governs one permission, or None when it is core.

    Returns the *band*, never the module: a band is what carries a depth, and
    the depth is the whole of what is being sold. A module key here would say
    only that the school holds Finance, which every school does.
    """
    band = band_for(module, resource, action)
    if band in (NEVER, PLATFORM):
        return None

    # Bulk work answers to one platform-wide feature wherever it appears, so
    # the named key wins before the resource's own module does.
    if (module, resource, action) in ACTION_CAPABILITY:
        return ACTION_CAPABILITY[(module, resource, action)]
    if module in MODULE_NAMED_CAPABILITY:
        return MODULE_NAMED_CAPABILITY[module]

    if band is None:
        return None

    named = NAMED_BAND.get((module, resource))
    if named:
        return named

    capability_module = (
        CAPABILITY_MODULE.get((module, resource))
        or CAPABILITY_MODULE_BY_MODULE.get(module)
    )
    if capability_module is None:
        # Banded inside a module nobody sells: core for every school.
        return None
    return f"{capability_module}_{_BAND_SUFFIX[band]}"
