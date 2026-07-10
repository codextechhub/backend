from django.db import migrations


FLAG_LABELS = {
    "modules.finance": "Finance and Billing",
    "modules.procurement": "Procurement and Vendor Management",
    "modules.attendance": "Attendance Management",
    "modules.gradebook": "Gradebook and Assessments",
    "modules.student_portal": "Student Portal",
    "modules.parent_portal": "Parent and Guardian Portal",
    "features.bulk_import": "Bulk Data Import",
    "features.data_export": "Data Export and Reporting",
    "features.sms_alerts": "SMS Notification Alerts",
    "features.email_alerts": "Email Notification Alerts",
}


def _capability_key(old_key):
    return old_key.split(".", 1)[-1]


def forwards(apps, schema_editor):
    ConfigurationKey = apps.get_model("vs_config", "ConfigurationKey")
    BranchConfigOverride = apps.get_model("vs_config", "BranchConfigOverride")
    BranchFeatureFlag = apps.get_model("vs_config", "BranchFeatureFlag")
    ConfigurationChangeLog = apps.get_model("vs_config", "ConfigurationChangeLog")
    Definition = apps.get_model("vs_config", "ConfigurationDefinition")
    Value = apps.get_model("vs_config", "ConfigurationValue")
    Capability = apps.get_model("vs_config", "Capability")
    CapabilityDependency = apps.get_model("vs_config", "CapabilityDependency")
    Entitlement = apps.get_model("vs_config", "CapabilityEntitlement")
    Override = apps.get_model("vs_config", "CapabilityOverride")
    Audit = apps.get_model("vs_config", "ConfigurationAuditEvent")
    XVSModules = apps.get_model("vs_schools", "XVSModules")
    SchoolPackageSetup = apps.get_model("vs_schools", "SchoolPackageSetup")

    for old in ConfigurationKey.objects.all():
        definition, _ = Definition.objects.get_or_create(
            key=old.key,
            defaults={
                "label": old.key.replace(".", " ").replace("_", " ").title(),
                "description": old.description,
                "value_type": "STRING",
                "allowed_scopes": ["platform", "school", "branch"],
                "sensitivity": "INTERNAL",
                "is_active": old.is_active,
                "created_by_id": old.created_by_id,
            },
        )
        Value.objects.get_or_create(
            definition=definition,
            scope_key="platform",
            defaults={"value": old.value, "updated_by_id": old.created_by_id},
        )

    for old in BranchConfigOverride.objects.select_related("branch"):
        definition, _ = Definition.objects.get_or_create(
            key=old.key,
            defaults={
                "label": old.key.replace(".", " ").replace("_", " ").title(),
                "description": f"Migrated configuration value for {old.key}.",
                "value_type": "STRING",
                "allowed_scopes": ["platform", "school", "branch"],
                "sensitivity": "INTERNAL",
            },
        )
        Value.objects.update_or_create(
            definition=definition,
            scope_key=f"branch:{old.branch_id}",
            defaults={
                "school_id": old.branch.school_id,
                "branch_id": old.branch_id,
                "value": old.value,
                "updated_by_id": old.updated_by_id,
            },
        )

    capabilities = {}
    for old in XVSModules.objects.all():
        capability, _ = Capability.objects.update_or_create(
            key=old.key,
            defaults={
                "label": old.name,
                "description": old.description,
                "kind": "MODULE",
                "requires_entitlement": True,
                "is_active": old.is_active,
            },
        )
        capabilities[old.key] = capability

    for old_key, label in FLAG_LABELS.items():
        key = _capability_key(old_key)
        capability, _ = Capability.objects.get_or_create(
            key=key,
            defaults={
                "label": label,
                "kind": "MODULE" if old_key.startswith("modules.") else "FEATURE",
                "requires_entitlement": old_key.startswith("modules."),
                "default_enabled": False,
            },
        )
        capabilities[key] = capability

    for key, required_keys in {
        "procurement": ["finance"],
        "parent_portal": ["student_portal"],
        "sms_alerts": ["finance"],
    }.items():
        capability = capabilities.get(key)
        if capability is None:
            continue
        for required_key in required_keys:
            required = capabilities.get(required_key)
            if required is not None:
                CapabilityDependency.objects.get_or_create(
                    capability=capability, requires=required
                )

    for setup in SchoolPackageSetup.objects.prefetch_related("enabled_modules"):
        for old_module in setup.enabled_modules.all():
            capability = capabilities.get(old_module.key)
            if capability:
                Entitlement.objects.update_or_create(
                    capability=capability,
                    scope_key=f"school:{setup.school_id}",
                    defaults={
                        "school_id": setup.school_id,
                        "state": "GRANTED", "source": "PACKAGE",
                    },
                )

    for old in BranchFeatureFlag.objects.select_related("branch"):
        capability = capabilities.get(_capability_key(old.flag_key))
        if capability is None:
            continue
        Override.objects.update_or_create(
            capability=capability,
            scope_key=f"branch:{old.branch_id}",
            defaults={
                "school_id": old.branch.school_id,
                "branch_id": old.branch_id,
                "state": "ENABLED" if old.is_enabled else "DISABLED",
                "updated_by_id": old.set_by_id,
            },
        )

    for old in ConfigurationChangeLog.objects.select_related("branch"):
        school_id = old.institution_id or (old.branch.school_id if old.branch_id else None)
        Audit.objects.create(
            action=f"legacy.{old.change_type.lower()}",
            target_type="LegacyConfiguration",
            target_id=old.target_key,
            actor_id=old.changed_by_id,
            school_id=school_id,
            branch_id=old.branch_id,
            scope_key=(
                f"branch:{old.branch_id}" if old.branch_id
                else f"school:{school_id}" if school_id else "platform"
            ),
            before_data={"value": old.previous_value},
            after_data={"value": old.new_value},
            reason=old.reason,
            metadata={"legacy_change_id": str(old.pk)},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_config", "0003_capability_configurationdefinition_and_more"),
        ("vs_schools", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
