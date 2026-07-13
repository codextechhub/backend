"""Backfill the new tenant scope from the legacy school scope.

For every scoped row that carried a ``school`` FK we (a) populate ``tenant``
from that school's canonical tenant and (b) rewrite the denormalized
``scope_key`` from its old ``school:<school_id>`` form to the new
``tenant:<tenant_id>`` form. Branch keys (``branch:<branch_id>``) and platform
rows (``platform``) are left untouched; branch rows still gain their tenant via
the branch's owning school.

The rewrite is a bijection: ``School.tenant`` is a OneToOne, so distinct schools
map to distinct tenants and no ``scope_key`` collision can occur. The step is
idempotent (re-running finds nothing left to rewrite) and reversible.

ConfigurationAuditEvent carries BEFORE UPDATE immutability triggers (installed
in 0006). We drop and reinstall them around the audit-table updates so the
history rows can be re-scoped exactly once, at migration time, without relaxing
the runtime guarantee.
"""
import importlib

from django.db import migrations


# Scoped tables that own a legacy school FK plus a denormalized scope_key.
_SCOPED_MODELS = [
    "ConfigurationValue",
    "CapabilityOverride",
    "ConfigurationAuditEvent",
    "CapabilityEntitlement",
]
_AUDIT_MODEL = "ConfigurationAuditEvent"
_IMMUTABILITY = "vs_config.migrations.0006_configuration_audit_immutability"


def _school_tenants(apps):
    School = apps.get_model("vs_schools", "School")
    return dict(School.objects.values_list("pk", "tenant_id"))


def forwards(apps, schema_editor):
    triggers = importlib.import_module(_IMMUTABILITY)
    school_tenants = _school_tenants(apps)
    for model_name in _SCOPED_MODELS:
        Model = apps.get_model("vs_config", model_name)
        # The audit table is UPDATE-blocked by DB triggers; lift them for the
        # one-time re-scope and reinstall immediately afterwards.
        if model_name == _AUDIT_MODEL:
            triggers.uninstall(apps, schema_editor)
        try:
            for school_id, tenant_id in school_tenants.items():
                rows = Model.objects.filter(school_id=school_id)
                rows.update(tenant_id=tenant_id)
                if tenant_id is not None:
                    rows.filter(scope_key=f"school:{school_id}").update(
                        scope_key=f"tenant:{tenant_id}"
                    )
        finally:
            if model_name == _AUDIT_MODEL:
                triggers.install(apps, schema_editor)


def backwards(apps, schema_editor):
    triggers = importlib.import_module(_IMMUTABILITY)
    school_tenants = _school_tenants(apps)
    for model_name in _SCOPED_MODELS:
        Model = apps.get_model("vs_config", model_name)
        if model_name == _AUDIT_MODEL:
            triggers.uninstall(apps, schema_editor)
        try:
            for school_id, tenant_id in school_tenants.items():
                rows = Model.objects.filter(school_id=school_id)
                if tenant_id is not None:
                    rows.filter(scope_key=f"tenant:{tenant_id}").update(
                        scope_key=f"school:{school_id}"
                    )
                rows.update(tenant_id=None)
        finally:
            if model_name == _AUDIT_MODEL:
                triggers.install(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [("vs_config", "0007_add_tenant_scope")]
    operations = [migrations.RunPython(forwards, backwards)]
