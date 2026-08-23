from django.db import migrations, models
from django.db.models import Count
from django.db.models.functions import Lower


def reject_duplicate_vendor_references(apps, schema_editor):
    """Fail before constraint creation when historical references collide."""
    VendorInvoice = apps.get_model("vs_procurement", "VendorInvoice")
    duplicates = list(
        VendorInvoice.objects.exclude(vendor_reference="")
        .annotate(reference_ci=Lower("vendor_reference"))
        .values("entity_id", "vendor_id", "reference_ci")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
        .order_by("entity_id", "vendor_id", "reference_ci")[:20]
    )
    if duplicates:
        examples = "; ".join(
            "entity={entity_id}, vendor={vendor_id}, reference={reference_ci!r}, rows={row_count}".format(
                **row,
            )
            for row in duplicates
        )
        raise RuntimeError(
            "Cannot add uniq_proc_vinvoice_entity_vendor_ref_ci: existing vendor "
            f"invoice references collide case-insensitively. Resolve them first. {examples}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0014_vendorassessment"),
    ]

    operations = [
        migrations.RunPython(
            reject_duplicate_vendor_references,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="vendorinvoice",
            constraint=models.UniqueConstraint(
                Lower("vendor_reference"),
                models.F("entity"),
                models.F("vendor"),
                condition=~models.Q(vendor_reference=""),
                name="uniq_proc_vinvoice_entity_vendor_ref_ci",
            ),
        ),
    ]
