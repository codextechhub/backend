"""Who approves a request can change while it waits, and the change is kept.

Adds a request's advance choice of approvers for a stage not yet open, the
append-only history of every person added to or taken off a stage, the person
a request is about, and two facts about a delegation: who set it up, and when
it reached the requests already waiting.

The existing delegations are described in the next migration, which is
kept apart because PostgreSQL cannot build this migration's indexes on a
table the same transaction has already updated.
"""
import django.db.models.deletion
import vs_workflow.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_tenants", "0010_remove_branch__type"),
        ("vs_workflow", "0021_who_really_acted_under_a_proxy"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkflowApproverChange",
            fields=[
                (
                    "id",
                    models.CharField(
                        default=vs_workflow.models._short_id,
                        editable=False,
                        max_length=8,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("ACTIVE_STAGE", "Active stage changed"),
                            ("UPCOMING_ASSIGNMENT", "Stage assigned before it opens"),
                            ("ASSIGNMENT_RESET", "Advance assignment removed"),
                            ("BULK_REPLACE", "Person replaced across requests"),
                            (
                                "DELEGATION_APPLIED",
                                "Delegation reached a waiting stage",
                            ),
                            (
                                "DELEGATION_REVOKED",
                                "Revoked delegation left a waiting stage",
                            ),
                        ],
                        max_length=30,
                    ),
                ),
                ("reason", models.TextField(blank=True, default="")),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-changed_at"],
            },
        ),
        migrations.CreateModel(
            name="WorkflowStageAssignment",
            fields=[
                (
                    "id",
                    models.CharField(
                        default=vs_workflow.models._short_id,
                        editable=False,
                        max_length=8,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("reason", models.CharField(max_length=500)),
                ("set_at", models.DateTimeField()),
            ],
        ),
        migrations.AddField(
            model_name="approvaldelegation",
            name="applied_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="approvaldelegation",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workflowinstance",
            name="request_for",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="workflowauditlog",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("INSTANCE_SUBMITTED", "Instance submitted"),
                    ("INSTANCE_WITHDRAWN", "Instance withdrawn by requester"),
                    ("INSTANCE_CANCELLED", "Instance cancelled by admin"),
                    ("INSTANCE_APPROVED", "Instance fully approved"),
                    ("INSTANCE_REJECTED", "Instance terminally rejected"),
                    ("INSTANCE_RETURNED", "Instance returned to requester"),
                    ("INSTANCE_RESUBMITTED", "Instance resubmitted after return"),
                    ("STAGE_ACTIVATED", "Stage became active"),
                    ("STAGE_APPROVED", "Stage approved"),
                    ("STAGE_REJECTED", "Stage rejected"),
                    (
                        "STAGE_SKIPPED_NO_APPROVER",
                        "Stage auto-skipped (no eligible approvers)",
                    ),
                    ("STAGE_SKIPPED_CONDITION", "Stage skipped (conditional branch)"),
                    ("APPROVER_ACTED", "An approver recorded a vote"),
                    ("ACTION_REVERSED", "Admin reversed an approver action"),
                    ("ROUTE_EVALUATED", "Route recomputed at stage transition"),
                    (
                        "APPROVERS_CHANGED",
                        "Admin changed who approves the active stage",
                    ),
                    (
                        "APPROVERS_ASSIGNED",
                        "Admin chose who approves a stage before it opens",
                    ),
                    (
                        "APPROVERS_ASSIGNMENT_RESET",
                        "Admin removed a stage's advance choice of approvers",
                    ),
                    ("DELEGATION_APPLIED", "A delegate joined a waiting stage"),
                    (
                        "DELEGATION_WITHDRAWN",
                        "A revoked delegation left a waiting stage",
                    ),
                ],
                db_index=True,
                max_length=50,
            ),
        ),
        migrations.AddIndex(
            model_name="approvaldelegation",
            index=models.Index(
                condition=models.Q(
                    ("applied_at__isnull", True), ("revoked_at__isnull", True)
                ),
                fields=["starts_at"],
                name="wf_delegation_unapplied_idx",
            ),
        ),
        migrations.AddField(
            model_name="workflowapproverchange",
            name="added_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workflowapproverchange",
            name="changed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workflowapproverchange",
            name="delegation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="vs_workflow.approvaldelegation",
            ),
        ),
        migrations.AddField(
            model_name="workflowapproverchange",
            name="instance",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="approver_changes",
                to="vs_workflow.workflowinstance",
            ),
        ),
        migrations.AddField(
            model_name="workflowapproverchange",
            name="on_behalf_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workflowapproverchange",
            name="removed_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workflowapproverchange",
            name="stage",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="vs_workflow.workflowstage",
            ),
        ),
        migrations.AddField(
            model_name="workflowapproverchange",
            name="stage_instance",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="vs_workflow.workflowstageinstance",
            ),
        ),
        migrations.AddField(
            model_name="workflowstageassignment",
            name="approvers",
            field=models.ManyToManyField(related_name="+", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="workflowstageassignment",
            name="instance",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="stage_assignments",
                to="vs_workflow.workflowinstance",
            ),
        ),
        migrations.AddField(
            model_name="workflowstageassignment",
            name="set_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workflowstageassignment",
            name="stage",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="vs_workflow.workflowstage",
            ),
        ),
        migrations.AddIndex(
            model_name="workflowapproverchange",
            index=models.Index(
                fields=["instance", "changed_at"], name="vs_workflow_instanc_4c5c27_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowstageassignment",
            constraint=models.UniqueConstraint(
                fields=("instance", "stage"), name="uniq_stage_assignment_per_instance"
            ),
        ),
    ]
