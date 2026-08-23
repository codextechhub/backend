from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vs_tickets", "0006_ticketsubscription"),
    ]

    operations = [
        migrations.CreateModel(
            name="GuideAnalyticsEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        choices=[
                            ("guide.viewed", "Guide viewed"),
                            ("guide.completed", "Guide completed"),
                            ("walkthrough.exited", "Walkthrough exited"),
                            ("guide.helpful_voted", "Helpful vote recorded"),
                            ("guide.outdated_reported", "Outdated guide reported"),
                            ("search.no_results", "Guide search returned no results"),
                        ],
                        db_index=True,
                        max_length=40,
                    ),
                ),
                ("guide_id", models.CharField(blank=True, default="", max_length=120)),
                ("walkthrough_id", models.CharField(blank=True, default="", max_length=140)),
                ("step_id", models.CharField(blank=True, default="", max_length=100)),
                (
                    "outcome",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("helpful", "Helpful"),
                            ("not_helpful", "Not helpful"),
                            ("finished", "Finished"),
                            ("paused", "Paused"),
                            ("target_unavailable", "Target unavailable"),
                        ],
                        default="",
                        max_length=24,
                    ),
                ),
                ("search_query", models.CharField(blank=True, default="", max_length=160)),
                ("route_pattern", models.CharField(blank=True, default="", max_length=200)),
                ("occurred_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                "db_table": "vs_tickets_guide_analytics_event",
                "ordering": ["-occurred_at"],
            },
        ),
        migrations.AddIndex(
            model_name="guideanalyticsevent",
            index=models.Index(
                fields=["name", "-occurred_at"],
                name="vst_gan_name_time_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="guideanalyticsevent",
            index=models.Index(
                fields=["guide_id", "-occurred_at"],
                name="vst_gan_guide_time_idx",
            ),
        ),
    ]
