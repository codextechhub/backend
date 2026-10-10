from django.db import migrations, models


TIER_PRICES = {
    "basic": 350_000,
    "standard": 500_000,
    "premium": 750_000,
}


def seed_subscription_terms(apps, schema_editor):
    PackagePlan = apps.get_model("vs_schools", "PackagePlan")
    SchoolPackageSetup = apps.get_model("vs_schools", "SchoolPackageSetup")

    for code, price in TIER_PRICES.items():
        PackagePlan.objects.filter(code=code).update(
            currency="NGN",
            price_per_student=price,
        )

    for setup in SchoolPackageSetup.objects.select_related("package_plan"):
        setup.subscription_starts_at = setup.created_at.date()
        setup.agreed_price_per_student = setup.package_plan.price_per_student
        setup.save(update_fields=[
            "subscription_starts_at",
            "agreed_price_per_student",
        ])


class Migration(migrations.Migration):

    dependencies = [
        ("vs_schools", "0013_the_library_carries_every_role_codex_ships"),
    ]

    operations = [
        migrations.AddField(
            model_name="school",
            name="email",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
        migrations.AddField(
            model_name="school",
            name="phone",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="packageplan",
            name="currency",
            field=models.CharField(
                choices=[("NGN", "Nigerian Naira"), ("USD", "US Dollar")],
                default="NGN",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="packageplan",
            name="price_per_student",
            field=models.PositiveBigIntegerField(
                blank=True,
                help_text=(
                    "Price per active student for one billing cycle, in the "
                    "currency's minor unit. Null means the school needs a quoted rate."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="schoolpackagesetup",
            name="agreed_price_per_student",
            field=models.PositiveBigIntegerField(
                blank=True,
                help_text=(
                    "The agreed per-student rate in minor currency units. It is copied "
                    "from the tier or entered for a quoted plan."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="schoolpackagesetup",
            name="minimum_billable_students",
            field=models.PositiveIntegerField(
                default=0,
                help_text=(
                    "Optional contracted minimum. Zero delays the first invoice until "
                    "the school has active enrolled students."
                ),
            ),
        ),
        migrations.AddField(
            model_name="schoolpackagesetup",
            name="subscription_starts_at",
            field=models.DateField(null=True),
        ),
        migrations.RunPython(seed_subscription_terms, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="schoolpackagesetup",
            name="subscription_starts_at",
            field=models.DateField(),
        ),
    ]
