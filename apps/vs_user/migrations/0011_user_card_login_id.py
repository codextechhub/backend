import uuid

from django.db import migrations, models


def issue_platform_card_ids(apps, schema_editor):
    User = apps.get_model("vs_user", "User")
    platform_users = User.objects.filter(tenant__kind="PLATFORM", card_login_id__isnull=True)
    for user in platform_users.iterator(chunk_size=500):
        user.card_login_id = uuid.uuid4()
        user.save(update_fields=["card_login_id"])


def revoke_platform_card_ids(apps, schema_editor):
    User = apps.get_model("vs_user", "User")
    User.objects.filter(tenant__kind="PLATFORM").update(card_login_id=None)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_user", "0010_request_bound_action_tokens"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="card_login_id",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.RunPython(issue_platform_card_ids, revoke_platform_card_ids),
        migrations.AlterField(
            model_name="autheventlog",
            name="event",
            field=models.CharField(
                choices=[
                    ("USER_CREATED", "User Created"),
                    ("INVITATION_SENT", "Invitation Sent"),
                    ("ACCOUNT_ACTIVATED", "Account Activated"),
                    ("LOGIN_SUCCESS", "Login Success"),
                    ("LOGIN_FAILURE", "Login Failure"),
                    ("TOKEN_REVOKED", "Token Revoked"),
                    ("FORCE_LOGOUT", "Force Logout"),
                    ("ACCOUNT_LOCKED", "Account Locked"),
                    ("ACCOUNT_UNLOCKED", "Account Unlocked"),
                    ("ACCOUNT_SUSPENDED", "Account Suspended"),
                    ("ACCOUNT_REACTIVATED", "Account Reactivated"),
                    ("ACCOUNT_DEACTIVATED", "Account Deactivated"),
                    ("PASSWORD_RESET_REQUESTED", "Password Reset Requested"),
                    ("PASSWORD_RESET_COMPLETED", "Password Reset Completed"),
                    ("PASSWORD_CHANGED", "Password Changed"),
                    ("EMAIL_CHANGED", "Email Changed"),
                    ("CARD_LOGIN_ROTATED", "Card Login Rotated"),
                ],
                max_length=40,
            ),
        ),
    ]
