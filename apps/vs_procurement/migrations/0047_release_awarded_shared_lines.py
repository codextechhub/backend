"""Release the allocations of shared RFQs already awarded.

An award hands each requisition line to the branch order it raised, and from
then on the order, not the RFQ, holds the line: cancelling the order frees it.
Awarding therefore releases the RFQ's allocations, and those of shared RFQs
awarded before that are released here, stamped with the moment the award was
recorded on the sourcing group.

Reversing leaves them released. Every line they held is on a branch order the
award raised, which holds it under either reading, and putting the hold back
could collide with a line since freed by a cancelled order and shared again.
"""

from django.db import migrations
from django.db.models import OuterRef, Subquery


def release_awarded_allocations(apps, schema_editor):
    """Release every still-held allocation whose shared RFQ has been awarded."""
    Allocation = apps.get_model("vs_procurement", "SharedSourcingAllocation")
    SharedSourcingGroup = apps.get_model("vs_procurement", "SharedSourcingGroup")
    awarded_at = SharedSourcingGroup.objects.filter(pk=OuterRef("group_id")).values("updated_at")[:1]
    Allocation.objects.filter(
        released_at__isnull=True, group__rfq__rfq_status="AWARDED",
    ).update(released_at=Subquery(awarded_at))


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0046_shared_sourcing_release"),
    ]

    operations = [
        migrations.RunPython(release_awarded_allocations, migrations.RunPython.noop),
    ]
