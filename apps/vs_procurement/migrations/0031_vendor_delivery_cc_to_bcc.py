"""Rename the vendor-delivery copy list from cc to bcc.

The addresses stored here are an internal monitoring mailbox, and they are now
delivered blind rather than visibly (see core.mail.send_email). Leaving the column
called ``cc`` would leave a field name asserting the opposite of what the system
does, which is how the next reader gets it wrong.

A rename, not a new column: the stored addresses are unchanged, only how they are
delivered, so existing delivery history keeps its recipients.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("vs_procurement", "0030_retarget_branch_to_vs_tenants"),
    ]

    operations = [
        migrations.RenameField(
            model_name="purchaseordervendordelivery",
            old_name="cc",
            new_name="bcc",
        ),
    ]
