"""Bind vendor quotation images to the tenant of their owning quotation."""

from django.db import migrations


def backfill_vendor_quotation_media_tenant(apps, schema_editor):
    """Repair live unscoped files only when their attachment binding still matches."""
    Attachment = apps.get_model("vs_procurement", "VendorQuotationAttachment")
    Quotation = apps.get_model("vs_procurement", "VendorQuotation")
    Entity = apps.get_model("vs_finance", "LedgerEntity")
    StoredFile = apps.get_model("core", "StoredFile")
    ContentType = apps.get_model("contenttypes", "ContentType")
    connection = schema_editor.connection
    content_type = ContentType.objects.using(connection.alias).get(
        app_label="vs_procurement", model="vendorquotationattachment",
    )
    quoted = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {quoted(StoredFile._meta.db_table)} AS stored
            SET tenant_id = entity.tenant_id
            FROM {quoted(Attachment._meta.db_table)} AS attachment
            JOIN {quoted(Quotation._meta.db_table)} AS quotation
              ON quotation.id = attachment.quotation_id
            JOIN {quoted(Entity._meta.db_table)} AS entity
              ON entity.id = quotation.entity_id
            WHERE stored.name = attachment.file
              AND quotation.vendor_managed = TRUE
              AND stored.owner_content_type_id = %s
              AND stored.owner_object_id = attachment.id::text
              AND stored.owner_field = 'file'
              AND stored.tenant_id IS NULL
              AND stored.revoked_at IS NULL
            """,
            [content_type.pk],
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0037_vendor_payment_wht_source"),
        ("core", "0007_backgroundjob_target_id"),
    ]

    operations = [
        migrations.RunPython(backfill_vendor_quotation_media_tenant, migrations.RunPython.noop),
    ]
