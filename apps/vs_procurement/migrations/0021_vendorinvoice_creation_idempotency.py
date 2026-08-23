from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0020_requestforquotation_response_due_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendorinvoice",
            name="creation_idempotency_key",
            field=models.CharField(
                blank=True,
                default="",
                editable=False,
                help_text="Client retry key used to return the original invoice instead of creating another.",
                max_length=128,
            ),
        ),
        migrations.AddField(
            model_name="vendorinvoice",
            name="creation_request_hash",
            field=models.CharField(
                blank=True,
                default="",
                editable=False,
                help_text="SHA-256 of the creation payload bound to the idempotency key.",
                max_length=64,
            ),
        ),
        migrations.AddConstraint(
            model_name="vendorinvoice",
            constraint=models.UniqueConstraint(
                condition=~models.Q(creation_idempotency_key=""),
                fields=("entity", "creation_idempotency_key"),
                name="uniq_proc_vinvoice_entity_idempotency_key",
            ),
        ),
    ]
