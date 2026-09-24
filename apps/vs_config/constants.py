# RBAC keys that protect configuration catalogue, value, and capability operations.
class ConfigPermissions:
    DEFINITION_VIEW = "config.definition.view"
    DEFINITION_CREATE = "config.definition.create"
    DEFINITION_UPDATE = "config.definition.update"
    DEFINITION_ARCHIVE = "config.definition.archive"
    VALUE_VIEW = "config.value.view"
    VALUE_UPDATE = "config.value.update"
    CAPABILITY_VIEW = "config.capability.view"
    CAPABILITY_CREATE = "config.capability.create"
    CAPABILITY_UPDATE = "config.capability.update"
    CAPABILITY_ARCHIVE = "config.capability.archive"
    ENTITLEMENT_VIEW = "config.entitlement.view"
    ENTITLEMENT_UPDATE = "config.entitlement.update"
    ENTITLEMENT_DELETE = "config.entitlement.delete"
    OVERRIDE_VIEW = "config.override.view"
    OVERRIDE_UPDATE = "config.override.update"
    AUDIT_VIEW = "config.audit.view"
    AUDIT_EXPORT = "config.audit.export"
    EXPORT_CREATE = "config.export.create"
    SECURITY_VIEW = "config.security.view"
    SECURITY_UPDATE = "config.security.update"
    INTEGRATION_VIEW = "config.integration.view"
    INTEGRATION_UPDATE = "config.integration.update"
    INTEGRATION_TRIGGER = "config.integration.trigger"

    # Seeding uses this list as the complete RBAC contract for the config module.
    ALL = [
        DEFINITION_VIEW, DEFINITION_CREATE, DEFINITION_UPDATE, DEFINITION_ARCHIVE,
        VALUE_VIEW, VALUE_UPDATE, CAPABILITY_VIEW, CAPABILITY_CREATE,
        CAPABILITY_UPDATE, CAPABILITY_ARCHIVE,
        ENTITLEMENT_VIEW, ENTITLEMENT_UPDATE, ENTITLEMENT_DELETE,
        OVERRIDE_VIEW, OVERRIDE_UPDATE,
        AUDIT_VIEW, AUDIT_EXPORT, EXPORT_CREATE,
        SECURITY_VIEW, SECURITY_UPDATE, INTEGRATION_VIEW, INTEGRATION_UPDATE,
        INTEGRATION_TRIGGER,
    ]


# Scope labels declared in ConfigurationDefinition.allowed_scopes. They name the
# LEVEL a value may be written at, not the persisted scope_key prefix: the middle
# level is labelled "school" while its stored key reads "tenant:<id>". A school IS
# a tenant, and the label is part of the definition payload's public shape.
PLATFORM_SCOPE = "platform"
SCHOOL_SCOPE = "school"
BRANCH_SCOPE = "branch"
VALID_SCOPES = {PLATFORM_SCOPE, SCHOOL_SCOPE, BRANCH_SCOPE}
