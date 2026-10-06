"""The words a person reads for every machine value this app returns.

The configuration API stores stable machine values: audit action codes
(``config.value.updated``), the class name of an audited record
(``ConfigurationValue``), enum members (``SECRET_REFERENCE``, ``MANUAL``),
scope keys (``tenant:<uuid>``) and dotted setting keys. Those values stay as
they are, because saved audit views, exports and filters are keyed on them.
What a screen shows beside them comes from here, so no client has to turn a
code identifier into a label, and every client reads the same words.

Each lookup has a neutral fallback ("Configuration change", "Other settings")
for a value this module does not know yet. A fallback never echoes the raw
value: a code identifier on screen is a defect, and a plain phrase that is
slightly too general is not.
"""

#: Audit action codes, as the audit trail and its filters name them.
AUDIT_ACTION_LABELS = {
    "config.value.updated": "Setting changed",
    "config.value.cleared": "Setting reset",
    "config.definition.created": "Setting created",
    "config.definition.updated": "Setting updated",
    "config.definition.archived": "Setting archived",
    "config.capability.created": "Feature created",
    "config.capability.updated": "Feature updated",
    "config.capability.archived": "Feature archived",
    "config.entitlement.updated": "Plan grant changed",
    "config.entitlement.cleared": "Plan grant reset",
    "config.depth_grant.updated": "Module level changed",
    "config.depth_grant.cleared": "Module level reset",
    "config.override.updated": "Forced status changed",
    "config.integration.connection_tested": "Connection tested",
    "config.audit.export_queued": "Audit export queued",
    "config.audit.export_completed": "Audit export completed",
    "config.audit.export_downloaded": "Audit export downloaded",
}
AUDIT_ACTION_FALLBACK = "Configuration change"

#: The kind of record an audit event names, keyed by the stored target type.
AUDIT_TARGET_TYPE_LABELS = {
    "ConfigurationDefinition": "Setting",
    "ConfigurationValue": "Setting value",
    "Capability": "Feature switch",
    "CapabilityEntitlement": "Plan grant",
    "CapabilityDepthGrant": "Module level grant",
    "CapabilityOverride": "Forced status",
    "IntegrationConnection": "Integration connection",
    "ConfigurationAuditExportJob": "Audit export",
}
AUDIT_TARGET_TYPE_FALLBACK = "Configuration record"

#: Field names inside an audit event's before and after snapshots.
AUDIT_FIELD_LABELS = {
    "value": "Value",
    "state": "Status",
    "source": "Granted through",
    "depth": "Level",
    "starts_at": "Starts",
    "ends_at": "Expires",
    "reason": "Reason",
    "status": "Export status",
    "filters": "Filters",
    "row_count": "Rows",
    "available_until": "Download available until",
    "id": "Record reference",
    "key": "Reference key",
    "label": "Name",
    "description": "Description",
    "value_type": "Kind of value",
    "default_value": "Default value",
    "validation_rules": "Validation rules",
    "allowed_scopes": "Where it can be set",
    "sensitivity": "Sensitivity",
    "is_active": "Active",
    "consumer": "Used by",
    "created_by": "Created by",
    "created_at": "Created",
    "updated_at": "Last updated",
    "kind": "Kind",
    "requires_entitlement": "Needs a plan grant",
    "default_enabled": "On by default",
    "metadata": "Additional details",
    "dependencies": "Requires",
    "tenant": "School",
    "branch": "Branch",
    "capability": "Feature",
}
AUDIT_FIELD_FALLBACK = "Other recorded detail"

#: Value types a setting definition may declare.
VALUE_TYPE_LABELS = {
    "STRING": "Text",
    "INTEGER": "Whole number",
    "DECIMAL": "Decimal number",
    "BOOLEAN": "On or off",
    "JSON": "Structured data (JSON)",
    "CHOICE": "One of a list",
    "SECRET_REFERENCE": "Secret reference",
}

#: What a person is told to type when a value is not of its setting's type,
#: worded from :data:`VALUE_TYPE_LABELS` so the refusal and the console agree.
VALUE_TYPE_INSTRUCTIONS = {
    "STRING": "Enter some text",
    "INTEGER": "Enter a whole number",
    "DECIMAL": "Enter a decimal number",
    "BOOLEAN": "Choose on or off",
    "JSON": "Enter structured data (JSON)",
    "CHOICE": "Choose one of the listed values",
    "SECRET_REFERENCE": "Enter a secret reference",
}

#: How carefully a setting's value is handled when it is read and audited.
SENSITIVITY_LABELS = {
    "PUBLIC": "Public",
    "INTERNAL": "Internal",
    "SECRET_REFERENCE": "Secret reference (never shown)",
}

#: The levels a setting may be given a value at, as ``allowed_scopes`` names them.
ALLOWED_SCOPE_LABELS = {
    "platform": "Platform-wide",
    "school": "Each school",
    "branch": "Each branch",
}

#: Capability kinds, as the singular word a record carries.
CAPABILITY_KIND_LABELS = {
    "MODULE": "Module",
    "FEATURE": "Feature",
}

#: Capability depth levels.
CAPABILITY_DEPTH_LABELS = {
    10: "Core",
    20: "Plus",
    30: "Advanced",
}

#: Entitlement states.
ENTITLEMENT_STATE_LABELS = {
    "GRANTED": "Granted",
    "DENIED": "Denied",
}

#: Where an entitlement came from.
ENTITLEMENT_SOURCE_LABELS = {
    "PACKAGE": "Plan package",
    "PLATFORM": "Platform default",
    "MANUAL": "Set by an operator",
    "IMPORT": "Imported",
}

#: Forced-status overrides.
OVERRIDE_STATE_LABELS = {
    "INHERIT": "Follows the plan",
    "ENABLED": "Forced on",
    "DISABLED": "Forced off",
}

#: Renewal calendar statuses.
CALENDAR_STATUS_LABELS = {
    "active": "Active",
    "scheduled": "Scheduled",
    "expired": "Expired",
}

#: Audit export job statuses.
EXPORT_JOB_STATUS_LABELS = {
    "QUEUED": "Queued",
    "RUNNING": "Running",
    "COMPLETED": "Completed",
    "FAILED": "Failed",
}

#: Integration connections an operator can test, by the code the endpoint takes.
INTEGRATION_CONNECTION_LABELS = {
    "email": "Email connection",
    "payments": "Payment provider connection",
}
INTEGRATION_CONNECTION_FALLBACK = "Integration connection"

#: Section headings for setting definitions, keyed by the first segment of the key.
SETTING_GROUP_LABELS = {
    "academics": "Academics",
    "calendar": "Calendar",
    "display": "Dates and times",
    "exams": "Exams",
    "finance": "Finance",
    "guardians": "Guardians",
    "integrations": "Integrations",
    "notifications": "Notifications",
    "payments": "Payments",
    "payroll": "Payroll",
    "platform": "Platform",
    "procurement": "Procurement",
    "security": "Security",
    "staff": "Staff",
    "students": "Students",
    "timetable": "Timetable",
    "workflow": "Approvals",
}
SETTING_GROUP_FALLBACK = "Other settings"


def _lookup(table, value, fallback):
    """*value*'s label in *table*, or *fallback* when it has none."""
    return table.get(value, fallback)


def audit_action_label(action):
    """The plain name of an audit action code."""
    return _lookup(AUDIT_ACTION_LABELS, action, AUDIT_ACTION_FALLBACK)


def audit_target_type_label(target_type):
    """The plain name of the kind of record an audit event names."""
    return _lookup(AUDIT_TARGET_TYPE_LABELS, target_type, AUDIT_TARGET_TYPE_FALLBACK)


def audit_field_label(field):
    """The plain name of a field inside an audit snapshot."""
    return _lookup(AUDIT_FIELD_LABELS, field, AUDIT_FIELD_FALLBACK)


def integration_connection_label(connection):
    """The plain name of an integration connection code (``email``, ``payments``)."""
    return _lookup(
        INTEGRATION_CONNECTION_LABELS, connection, INTEGRATION_CONNECTION_FALLBACK,
    )


def setting_group_label(key):
    """The section heading a setting definition is listed under.

    The heading follows the first segment of the dotted key, because that
    segment names the area that reads the setting. A key whose area has no
    heading here is listed under "Other settings" rather than under its raw
    prefix.
    """
    prefix = key.split(".", 1)[0] if "." in (key or "") else ""
    return _lookup(SETTING_GROUP_LABELS, prefix, SETTING_GROUP_FALLBACK)


def payment_provider_label(code):
    """The provider's own name for a payment provider code (``PAYSTACK``)."""
    from vs_payments.constants import PaymentProvider

    return dict(PaymentProvider.choices).get(code, "Payment provider")


def value_source_label(source):
    """Where a setting's effective value comes from, in words.

    *source* is the :class:`~vs_config.models.ConfigurationValue` row that
    supplied the value, or None when the definition's built-in default did.
    A school's or a branch's row is named after that school or branch, so an
    operator reading "Reset to ..." knows exactly which value now applies.
    """
    if source is None:
        return "Built-in default"
    if source.branch_id:
        return f"{source.branch.name} branch value"
    if source.tenant_id:
        return f"{source.tenant.name} value"
    return "Platform value"


def choice_options(labels):
    """A label table as the ``[{value, label}]`` list a picker renders."""
    return [{"value": value, "label": label} for value, label in labels.items()]
