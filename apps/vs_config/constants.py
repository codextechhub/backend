# RBAC keys that protect configuration catalogue, value, and capability operations.
class ConfigPermissions:
    DEFINITION_VIEW = "config.definition.view"
    DEFINITION_CREATE = "config.definition.create"
    DEFINITION_UPDATE = "config.definition.update"
    DEFINITION_ARCHIVE = "config.definition.archive"
    VALUE_VIEW = "config.value.view"
    VALUE_UPDATE = "config.value.update"
    CAPABILITY_VIEW = "config.capability.view"
    CAPABILITY_MANAGE = "config.capability.manage"
    ENTITLEMENT_VIEW = "config.entitlement.view"
    ENTITLEMENT_MANAGE = "config.entitlement.manage"
    OVERRIDE_VIEW = "config.override.view"
    OVERRIDE_MANAGE = "config.override.manage"
    AUDIT_VIEW = "config.audit.view"
    AUDIT_EXPORT = "config.audit.export"
    EXPORT_CREATE = "config.export.create"
    SECURITY_VIEW = "config.security.view"
    SECURITY_MANAGE = "config.security.manage"
    INTEGRATION_VIEW = "config.integration.view"
    INTEGRATION_MANAGE = "config.integration.manage"

    # Seeding uses this list as the complete RBAC contract for the config module.
    ALL = [
        DEFINITION_VIEW, DEFINITION_CREATE, DEFINITION_UPDATE, DEFINITION_ARCHIVE,
        VALUE_VIEW, VALUE_UPDATE, CAPABILITY_VIEW, CAPABILITY_MANAGE,
        ENTITLEMENT_VIEW, ENTITLEMENT_MANAGE, OVERRIDE_VIEW, OVERRIDE_MANAGE,
        AUDIT_VIEW, AUDIT_EXPORT, EXPORT_CREATE,
        SECURITY_VIEW, SECURITY_MANAGE, INTEGRATION_VIEW, INTEGRATION_MANAGE,
    ]


# Definition-level scope labels declared in ConfigurationDefinition.allowed_scopes.
# These name the LEVEL a value may be written at, not the persisted scope_key
# prefix. The middle level keeps the historical label "school" (a school IS a
# tenant) so definition payloads/response shapes stay stable across the tenant
# cutover, even though the stored scope_key now reads "tenant:<id>".
PLATFORM_SCOPE = "platform"
SCHOOL_SCOPE = "school"
BRANCH_SCOPE = "branch"
VALID_SCOPES = {PLATFORM_SCOPE, SCHOOL_SCOPE, BRANCH_SCOPE}
