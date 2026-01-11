from django.core.management.base import BaseCommand
import random
import string
from django.utils import timezone
from ...models import Product, Institution, GlobalUser, UserRole, PlatformStaff, InstitutionUser, UserActivityLog, APIKey, SystemSetting
from ...utils import get_uniqueid as get_uid


class Command(BaseCommand):
    help = 'Populate the database with a single record for each model'

    def handle(self, *args, **kwargs):
        # Create a single product
        product = Product.objects.create(
            name="Vision",
            description="A sample product for Vision."
        )
        self.stdout.write(self.style.SUCCESS(f"Created product: {product.name}"))

        # Create a single institution for the product
        institution = Institution.objects.create(
            name="Vision Institution",
            slug="v-ins",
            product=product,
            is_active=True
        )
        self.stdout.write(self.style.SUCCESS(f"Created institution: {institution.name}"))

        # Create a single user role
        role = UserRole.objects.create(
            name="Admin",
            description="Role for Admin users."
        )
        self.stdout.write(self.style.SUCCESS(f"Created user role: {role.name}"))

        # Create a single global user
        global_user = GlobalUser.objects.create(
            username="admin@vision.com",
            first_name="Admin",
            last_name="User",
            institution=institution,
            uid=get_uid(institution)
        )
        # set a usable password (will be hashed)
        global_user.set_password("ChangeMe123!")
        global_user.save()
        self.stdout.write(self.style.SUCCESS(f"Created global user: {global_user.username}"))

        # Create a single platform staff
        platform_staff = PlatformStaff.objects.create(
            global_user=global_user,
            role=role,
            position="Product Manager"
        )
        self.stdout.write(self.style.SUCCESS(f"Created platform staff: {platform_staff.global_user.username}"))

        # Create a single activity log for platform staff
        UserActivityLog.objects.create(
            staff=InstitutionUser.objects.get(global_user__id=1).global_user,
            action="Logged in",
            timestamp=timezone.now(),
            ip_address="192.168.1.1",
            user_agent="Mozilla/5.0"
        )
        self.stdout.write(self.style.SUCCESS(f"Created activity log for staff {InstitutionUser.objects.get(global_user__id=1).global_user.username}"))

        # Create a single API key
        api_key = APIKey.objects.create(
            key=self.generate_api_key(),
            is_active=True,
            expires_at=timezone.now() + timezone.timedelta(days=365)
        )
        self.stdout.write(self.style.SUCCESS(f"Created API key: {api_key.key}"))

        # Create a single system setting
        system_setting = SystemSetting.objects.create(
            key="MAX_LOGIN_ATTEMPTS",
            value="5",
            description="Maximum number of login attempts before lockout."
        )
        self.stdout.write(self.style.SUCCESS(f"Created system setting: {system_setting.key}"))

    def generate_api_key(self):
        """Generate a random API key."""
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=32))
