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
    EXPORT_CREATE = "config.export.create"

    # Seeding uses this list as the complete RBAC contract for the config module.
    ALL = [
        DEFINITION_VIEW, DEFINITION_CREATE, DEFINITION_UPDATE, DEFINITION_ARCHIVE,
        VALUE_VIEW, VALUE_UPDATE, CAPABILITY_VIEW, CAPABILITY_MANAGE,
        ENTITLEMENT_VIEW, ENTITLEMENT_MANAGE, OVERRIDE_VIEW, OVERRIDE_MANAGE,
        AUDIT_VIEW, EXPORT_CREATE,
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
