"""Backfill RequestMetric.tenant from the legacy school FK.

For every rollup row that carried a ``school`` we populate ``tenant`` from that
school's canonical tenant (``School.tenant``, a OneToOne, so distinct schools map
to distinct tenants). Rows with no school (platform-anonymous traffic) are left
null. Idempotent (re-running finds nothing new) and reversible.
"""
from django.db import migrations


def _school_tenants(apps):
    School = apps.get_model("vs_schools", "School")
    return dict(School.objects.values_list("pk", "tenant_id"))


def forwards(apps, schema_editor):
    RequestMetric = apps.get_model("vs_health", "RequestMetric")
    for school_id, tenant_id in _school_tenants(apps).items():
        if tenant_id is None:
            continue
        RequestMetric.objects.filter(school_id=school_id).update(tenant_id=tenant_id)


def backwards(apps, schema_editor):
    RequestMetric = apps.get_model("vs_health", "RequestMetric")
    RequestMetric.objects.exclude(tenant_id=None).update(tenant_id=None)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_health", "0002_requestmetric_tenant"),
        ("vs_schools", "0004_require_school_tenant"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
