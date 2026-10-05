"""Let a shared RFQ that ends without award give its requisition lines back.

An allocation gains ``released_at``. Only an unreleased allocation holds its
requisition line, so the one-allocation-per-line rule becomes one unreleased
allocation per line. Shared RFQs already cancelled or closed are released here,
stamped with the moment the RFQ last changed, which is when it ended.

Reversing restores the unconditional rule, and fails if a released line has
since been put on another shared RFQ: undoing that would need one of the two
RFQs to lose its allocation.
"""

from django.db import migrations, models
from django.db.models import OuterRef, Subquery

ENDED_WITHOUT_AWARD = ("CANCELLED", "CLOSED")


def release_ended_allocations(apps, schema_editor):
    """Release every allocation whose shared RFQ was cancelled or closed without award."""
    Allocation = apps.get_model("vs_procurement", "SharedSourcingAllocation")
    RequestForQuotation = apps.get_model("vs_procurement", "RequestForQuotation")
    ended_at = RequestForQuotation.objects.filter(
        shared_sourcing_group=OuterRef("group_id"),
    ).values("updated_at")[:1]
    Allocation.objects.filter(
        released_at__isnull=True, group__rfq__rfq_status__in=ENDED_WITHOUT_AWARD,
    ).update(released_at=Subquery(ended_at))


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0045_shared_sourcing_groups"),
    ]

    operations = [
        migrations.AddField(
            model_name="sharedsourcingallocation",
            name="released_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(release_ended_allocations, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="sharedsourcingallocation",
            name="uniq_shared_sourcing_source_line",
        ),
        migrations.AddConstraint(
            model_name="sharedsourcingallocation",
            constraint=models.UniqueConstraint(
                condition=models.Q(released_at__isnull=True),
                fields=("requisition_line",),
                name="uniq_shared_sourcing_live_source_line",
            ),
        ),
    ]
