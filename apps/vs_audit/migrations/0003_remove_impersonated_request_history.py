from django.db import migrations, models


def remove_request_level_proxy_history(apps, schema_editor):
    """Remove the noisy request-per-page records and their cached trails."""
    AuditEvent = apps.get_model("vs_audit", "AuditEvent")
    EntityAuditTrail = apps.get_model("vs_audit", "EntityAuditTrail")

    AuditEvent.objects.filter(action_type="IMPERSONATED_REQUEST").delete()
    # APIRequest was used exclusively by the removed proxy request middleware.
    EntityAuditTrail.objects.filter(entity_type="APIRequest").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_audit", "0002_initial"),
    ]

    operations = [
        migrations.RunPython(
            remove_request_level_proxy_history,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="action_type",
            field=models.CharField(
                choices=[
                    ("CREATE", "Create"),
                    ("UPDATE", "Update"),
                    ("DELETE", "Delete"),
                    ("USER_CREATED", "User Created"),
                    ("USER_INVITED", "User Invited"),
                    ("ACCOUNT_ACTIVATED", "Account Activated"),
                    ("LOGIN_SUCCESS", "Login Success"),
                    ("LOGIN_FAILED", "Login Failed"),
                    ("TOKEN_REVOKED", "Token Revoked"),
                    ("FORCE_LOGOUT", "Force Logout"),
                    ("ACCOUNT_LOCKED", "Account Locked"),
                    ("ACCOUNT_UNLOCKED", "Account Unlocked"),
                    ("ACCOUNT_SUSPENDED", "Account Suspended"),
                    ("ACCOUNT_REACTIVATED", "Account Reactivated"),
                    ("ACCOUNT_DEACTIVATED", "Account Deactivated"),
                    ("PASSWORD_RESET_REQUESTED", "Password Reset Requested"),
                    ("PASSWORD_RESET", "Password Reset"),
                    ("PASSWORD_CHANGED", "Password Changed"),
                    ("EMAIL_CHANGED", "Email Changed"),
                    ("DATA_FILE_UPLOADED", "Data File Uploaded"),
                    ("DATA_IMPORT_STARTED", "Data Import Started"),
                    ("DATA_IMPORT_ROW_PROCESSED", "Data Import Row Processed"),
                    ("DATA_IMPORT_COMPLETED", "Data Import Completed"),
                    ("DATA_IMPORT_FAILED", "Data Import Failed"),
                    ("DATA_IMPORT_ROLLED_BACK", "Data Import Rolled Back"),
                    ("ROLE_ASSIGNED", "Role Assigned"),
                    ("ROLE_CHANGED", "Role Changed"),
                    ("PERMISSION_CHANGED", "Permission Changed"),
                    ("IMPERSONATION_STARTED", "Impersonation Started"),
                    ("IMPERSONATION_ENDED", "Impersonation Ended"),
                    ("PROXY_CHANGE", "Change Through Proxy"),
                    ("PROXY_ACTION_FAILED", "Proxy Action Failed"),
                    ("CONFIG_CHANGED", "Configuration Changed"),
                    ("FINANCIAL_TRANSACTION", "Financial Transaction"),
                    ("PROCUREMENT_ACTION", "Procurement Action"),
                    ("EXPORT_REQUESTED", "Export Requested"),
                    ("EXPORT_COMPLETED", "Export Completed"),
                    ("EXPORT_FAILED", "Export Failed"),
                    ("CUSTOM", "Custom"),
                ],
                db_index=True,
                max_length=80,
            ),
        ),
    ]
