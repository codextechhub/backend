"""Bind account-action tokens to their exact request rows.

Existing invitation URLs remain valid: their former User.activation_key value
is hashed onto UserInvitation before the raw user field is removed. Existing
password-reset URLs cannot be mapped safely to one reset row, so outstanding
legacy reset rows are consumed during deployment.
"""

import secrets

from django.db import migrations, models
from django.utils import timezone
from django.utils.crypto import salted_hmac


def bind_existing_action_tokens(apps, schema_editor):
    UserInvitation = apps.get_model("vs_user", "UserInvitation")
    PasswordResetRequest = apps.get_model("vs_user", "PasswordResetRequest")

    invitations = []
    for invitation in UserInvitation.objects.select_related("user").iterator():
        invitation.token_hash = salted_hmac(
            "vs_user.invitation",
            str(invitation.user.activation_key),
            algorithm="sha256",
        ).hexdigest()
        invitations.append(invitation)
        if len(invitations) == 500:
            UserInvitation.objects.bulk_update(invitations, ["token_hash"])
            invitations = []
    if invitations:
        UserInvitation.objects.bulk_update(invitations, ["token_hash"])

    now = timezone.now()
    resets = []
    for reset in PasswordResetRequest.objects.all().iterator():
        # No legacy reset row identifies the URL that created it. Give every
        # historical row an unreachable unique digest and consume live rows.
        reset.token_hash = salted_hmac(
            "vs_user.password_reset",
            secrets.token_urlsafe(32),
            algorithm="sha256",
        ).hexdigest()
        if reset.used_at is None:
            reset.used_at = now
        resets.append(reset)
        if len(resets) == 500:
            PasswordResetRequest.objects.bulk_update(
                resets, ["token_hash", "used_at"],
            )
            resets = []
    if resets:
        PasswordResetRequest.objects.bulk_update(
            resets, ["token_hash", "used_at"],
        )


class Migration(migrations.Migration):

    dependencies = [
        ("vs_user", "0009_drop_user_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="userinvitation",
            name="token_hash",
            field=models.CharField(
                editable=False, max_length=64, null=True,
            ),
        ),
        migrations.AddField(
            model_name="passwordresetrequest",
            name="token_hash",
            field=models.CharField(
                editable=False, max_length=64, null=True,
            ),
        ),
        migrations.RunPython(
            bind_existing_action_tokens,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="userinvitation",
            name="token_hash",
            field=models.CharField(
                editable=False, max_length=64, unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="passwordresetrequest",
            name="token_hash",
            field=models.CharField(
                editable=False, max_length=64, unique=True,
            ),
        ),
        migrations.RemoveField(
            model_name="user",
            name="activation_key",
        ),
    ]
