from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("vs_user", "0004_require_user_tenant")]
    operations = [migrations.RemoveField(model_name="user", name="persona")]
