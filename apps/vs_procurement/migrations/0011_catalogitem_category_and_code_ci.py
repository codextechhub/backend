import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models


def normalize_catalog_codes(apps, schema_editor):
    """Canonicalize legacy codes before installing the race-safe CI constraint."""
    CatalogItem = apps.get_model("vs_procurement", "CatalogItem")
    seen = set()
    for item in CatalogItem.objects.order_by("entity_id", "id"):
        code = str(item.code or "").strip().upper()
        key = (item.entity_id, code)
        if key in seen:
            raise RuntimeError(
                f"Duplicate catalog item code after normalization: entity={item.entity_id}, code={code}"
            )
        seen.add(key)
        CatalogItem.objects.filter(pk=item.pk).update(code=code)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0010_vendorcategory_parent_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="catalogitem",
            name="category",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional purchasing taxonomy classification for this item.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="catalog_items",
                to="vs_procurement.vendorcategory",
            ),
        ),
        migrations.RunPython(normalize_catalog_codes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="catalogitem",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("code"),
                models.F("entity"),
                name="uniq_proc_catalogitem_entity_code_ci",
            ),
        ),
        migrations.AddIndex(
            model_name="catalogitem",
            index=models.Index(
                fields=["entity", "category"],
                name="proc_catalog_ent_cat_idx",
            ),
        ),
    ]
