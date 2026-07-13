# vs_users/management/commands/create_superuser.py
# ENHANCED VERSION — with optional defaults for quick bootstrap
#
# Usage with defaults:
#   python manage.py create_superuser
#   (Creates: admin@codexvision.com with default password)
#
# Usage with custom values:
#   python manage.py create_superuser \
#     --email custom@email.com \
#     --password "CustomP@ss" \
#     --first-name Custom \
#     --last-name Admin

from django.core.management.base import BaseCommand
from django.db import transaction

from vs_user.models import User
from vs_user.services.audit import log_auth_event
from vs_user.models import AuthEventLog
from vs_rbac.models import (
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)


def _codex_tenant():
    """Return the codex platform tenant, or None if migrations have not run."""
    from vs_tenants.models import Tenant
    return Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()


def _assign_super_admin(user):
    """Grant the codex xvs_super_admin tenant role to *user* (idempotent).

    Returns True on success, False if the codex tenant / role is missing.
    """
    codex = _codex_tenant()
    if codex is None:
        return False
    role, _ = TenantRoleTemplate.objects.get_or_create(
        tenant=codex,
        key="xvs_super_admin",
        defaults={
            "name": "XVS Super Admin",
            "status": "ACTIVE",
            "is_system_role": True,
            "is_locked": True,
        },
    )
    assignment = TenantUserRoleAssignment.objects.filter(
        tenant=codex, user=user, role=role,
    ).first()
    if assignment is None:
        TenantUserRoleAssignment.objects.create(
            tenant=codex, user=user, role=role,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            assigned_by=None,
        )
        return True
    if assignment.assignment_status == TenantUserRoleAssignment.AssignmentStatus.REVOKED:
        assignment.assignment_status = TenantUserRoleAssignment.AssignmentStatus.ACTIVE
        assignment.revoked_at = None
        assignment.revoked_by = None
        assignment.save(update_fields=["assignment_status", "revoked_at", "revoked_by", "updated_at"])
    return True


class Command(BaseCommand):
    help = 'Creates the first Vision Staff superuser for the CodeX Vision platform'
    
    # ═════════════════════════════════════════════════════════════════════════
    # DEFAULTS CONFIGURATION
    # ═════════════════════════════════════════════════════════════════════════
    # Change these values to customize your default superuser
    
    DEFAULT_EMAIL      = 'admin@codexng.com'  # ⚠️ Change in production!
    DEFAULT_PASSWORD   = 'Admin@123456'  # ⚠️ Change in production!
    DEFAULT_FIRST_NAME = 'System'
    DEFAULT_LAST_NAME  = 'Administrator'
    DEFAULT_PHONE      = ''
    
    # Option: Read defaults from environment variables (more secure)
    # Uncomment these to use env vars instead of hardcoded values:
    # import os
    # DEFAULT_EMAIL      = os.getenv('SUPERUSER_EMAIL', 'admin@codexng.com')
    # DEFAULT_PASSWORD   = os.getenv('SUPERUSER_PASSWORD', 'Admin@123456')
    # DEFAULT_FIRST_NAME = os.getenv('SUPERUSER_FIRST_NAME', 'System')
    # DEFAULT_LAST_NAME  = os.getenv('SUPERUSER_LAST_NAME', 'Administrator')
    
    # ═════════════════════════════════════════════════════════════════════════
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            type=str,
            default=self.DEFAULT_EMAIL,
            help=f'Superuser email address (default: {self.DEFAULT_EMAIL})',
        )
        parser.add_argument(
            '--password',
            type=str,
            default=self.DEFAULT_PASSWORD,
            help='Superuser password (default: uses preset value)',
        )
        parser.add_argument(
            '--first-name',
            type=str,
            default=self.DEFAULT_FIRST_NAME,
            help=f'First name (default: {self.DEFAULT_FIRST_NAME})',
        )
        parser.add_argument(
            '--last-name',
            type=str,
            default=self.DEFAULT_LAST_NAME,
            help=f'Last name (default: {self.DEFAULT_LAST_NAME})',
        )
        parser.add_argument(
            '--phone',
            type=str,
            default=self.DEFAULT_PHONE,
            help='Phone number (optional)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Skip the "Vision Staff already exists" check (use with caution)',
        )
        parser.add_argument(
            '--interactive',
            action='store_true',
            help='Prompt for values instead of using defaults',
        )
        parser.add_argument(
            '--assign-role',
            action='store_true',
            help='Skip user creation — just assign Vision Super Admin role to an existing user (use with --email)',
        )
    
    @transaction.atomic
    def handle(self, *args, **options):
        # ── Assign-role-only mode ─────────────────────────────────────────────
        if options['assign_role']:
            self._assign_role_to_existing(options)
            return

        # ── Bootstrap permission management capability ─────────────────────────
        self._bootstrap_permission_creation_capability()

        # ── Interactive Mode ──────────────────────────────────────────────────

        if options['interactive']:
            email      = self._prompt('Email', self.DEFAULT_EMAIL)
            first_name = self._prompt('First Name', self.DEFAULT_FIRST_NAME)
            last_name  = self._prompt('Last Name', self.DEFAULT_LAST_NAME)
            phone      = self._prompt('Phone (optional)', '')
            password   = self._prompt_password()
        else:
            email      = options['email'].strip().lower()
            password   = options['password']
            first_name = options['first_name'].strip()
            last_name  = options['last_name'].strip()
            phone      = options['phone'].strip()
        
        force = options['force']
        
        # ── Display Configuration ─────────────────────────────────────────────
        
        self.stdout.write('\n' + '═' * 60)
        self.stdout.write(self.style.MIGRATE_HEADING('  CodeX Vision — Superuser Creation'))
        self.stdout.write('═' * 60 + '\n')
        
        self.stdout.write(self.style.WARNING('Creating superuser with the following details:\n'))
        self.stdout.write(f'  Email:      {email}')
        self.stdout.write(f'  Name:       {first_name} {last_name}')
        self.stdout.write(f'  Phone:      {phone or "(none)"}')
        self.stdout.write(f'  Password:   {"*" * len(password)}\n')
        
        # ── Validation ────────────────────────────────────────────────────────
        
        existing_count, user_exist, len_pass = None, None, None
        # Check if Vision Staff already exists (unless --force is used)
        if not force:
            existing_count = User.objects.filter(user_type=User.UserType.CX_STAFF).count()
        
        # Check for duplicate email
        if User.objects.filter(email__iexact=email).exists():
            user_exist = True
        
        # Validate password length
        if len(password) < 8:
            len_pass = True
        
        # ── Create Superuser ──────────────────────────────────────────────────
        
        if existing_count is not None and existing_count > 0:
            self.stdout.write(self.style.ERROR('An XVS Staff superuser already exists. Use --force to override.'))
            return
        
        if user_exist:  
            self.stdout.write(self.style.ERROR('A user with this email already exists. Please choose a different email.'))
            return
        
        if len_pass:
            self.stdout.write(self.style.ERROR('Password must be at least 8 characters long. Please choose a stronger password.'))
            return
        
        self.stdout.write(self.style.MIGRATE_LABEL('\n⏳ Creating Vision Staff superuser...'))
        
        user = User.objects.create_user(
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            user_type=User.UserType.CX_STAFF,
            status=User.Status.ACTIVE,
            is_active=True,
            is_staff=True,      # Django admin access
            is_superuser=True,  # Django admin superuser permissions
            school=None,        # Vision Staff have no school assignment
            branch=None,        # Vision Staff have no branch assignment
            invited_by=None,    # Self-created (bootstrap account)
        )
        
        # ── Assign XVS Super Admin role (codex tenant) ────────────────────────
        if not _assign_super_admin(user):
            self.stdout.write(self.style.WARNING(
                "  ⚠️  Codex tenant / 'xvs_super_admin' role not found — run migrations first."
            ))

        # ── Audit Log ─────────────────────────────────────────────────────────
        log_auth_event(
            actor=None,
            subject=user,
            school=None,
            event=AuthEventLog.Event.USER_CREATED,
            metadata={
                'bootstrap': True,
                'user_type': User.UserType.CX_STAFF,
                'is_superuser': True,
                'created_via': 'management_command',
                'used_defaults': not options['interactive'],
            },
        )
        
        # ── Success Message ───────────────────────────────────────────────────
        
        self.stdout.write('\n' + '═' * 60)
        self.stdout.write(self.style.SUCCESS('  ✅ Superuser Created Successfully!'))
        self.stdout.write('═' * 60 + '\n')
        
        self.stdout.write(self.style.MIGRATE_LABEL('Account Details:'))
        self.stdout.write(f'  Email:      {user.email}')
        self.stdout.write(f'  Name:       {user.full_name}')
        self.stdout.write(f'  User Type:  {user.user_type}')
        self.stdout.write(f'  Status:     {user.status}')
        self.stdout.write(f'  Role:       XVS Super Admin (can create permissions on onset)')
        self.stdout.write(f'  ID:         {user.id}')
        
        self.stdout.write('\n' + self.style.MIGRATE_LABEL('Login Information:'))
        self.stdout.write(f'  URL:        /api/v1/auth/login/')
        self.stdout.write(f'  Email:      {user.email}')
        self.stdout.write(f'  Password:   {"*" * len(password)}')
        
        self.stdout.write('\n' + self.style.MIGRATE_LABEL('Next Steps:'))
        self.stdout.write('  1. Test login via API')
        self.stdout.write('  2. Access Django admin at /admin/')
        self.stdout.write('  3. Create additional Vision Staff accounts')
        self.stdout.write('  4. Never share these credentials\n')
        
        # Show login test command
        self.stdout.write(self.style.WARNING('Test login with:'))
        self.stdout.write(
            f'  curl -X POST http://localhost:8000/api/v1/auth/login/ \\\n'
            f'    -H "Content-Type: application/json" \\\n'
            f'    -d \'{{"email":"{user.email}","password":"YOUR_PASSWORD"}}\'\n'
        )
    
    # ── Helper Methods ────────────────────────────────────────────────────────

    def _bootstrap_permission_creation_capability(self):
        """Ensure the codex platform roles exist and carry the full platform set.

        Delegates to ``seed_platform_permissions`` — the single source of truth
        for the platform permission keys (organogram, schools, audit, …) and
        their grants. That command idempotently get_or_creates the codex-tenant
        ``xvs_super_admin`` / ``xvs_platform_admin`` roles (required by
        ``transfer_super_admin``) and grants the permissions onto them.

        Safe to re-run: everything uses get_or_create.
        """
        from django.core.management import call_command

        try:
            call_command('seed_platform_permissions', stdout=self.stdout, stderr=self.stderr)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"⚠️  Permission bootstrap: {e}"))

    def _assign_role_to_existing(self, options):
        email = options['email'].strip().lower()

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"No user found with email: {email}"))
            return

        if _assign_super_admin(user):
            self.stdout.write(self.style.SUCCESS(
                f"  ✅ Vision Super Admin role assigned/active for {user.email}"
            ))
        else:
            self.stdout.write(self.style.ERROR(
                "Codex tenant / 'xvs_super_admin' role not found. Run migrations "
                "and seed_platform_permissions first."
            ))

    def _prompt(self, field_name, default):
        """Prompt user for input with a default value."""
        if default:
            value = input(f'{field_name} [{default}]: ').strip()
            return value if value else default
        else:
            value = input(f'{field_name}: ').strip()
            return value
    
    def _prompt_password(self):
        """Prompt for password with confirmation."""
        import getpass
        while True:
            password = getpass.getpass('Password: ')
            if len(password) < 8:
                self.stdout.write(self.style.ERROR('Password must be at least 8 characters.'))
                continue
            confirm = getpass.getpass('Confirm password: ')
            if password != confirm:
                self.stdout.write(self.style.ERROR('Passwords do not match.'))
                continue
            return password


# =============================================================================
# USAGE EXAMPLES
# =============================================================================
#
# 1. Use all defaults (FASTEST — one command!):
#    python manage.py create_superuser
#
#    Creates:
#      Email:    admin@codexvision.com
#      Password: Admin@123456
#      Name:     System Administrator
#
# ─────────────────────────────────────────────────────────────────────────────
#
# 2. Override email only (keep other defaults):
#    python manage.py create_superuser --email custom@email.com
#
# ─────────────────────────────────────────────────────────────────────────────
#
# 3. Override password only (keep other defaults):
#    python manage.py create_superuser --password "MySecureP@ss"
#
# ─────────────────────────────────────────────────────────────────────────────
#
# 4. Customize everything:
#    python manage.py create_superuser \
#      --email admin@myschool.com \
#      --password "SecureP@ss123" \
#      --first-name John \
#      --last-name Doe \
#      --phone "+2348012345678"
#
# ─────────────────────────────────────────────────────────────────────────────
#
# 5. Interactive mode (prompts for each value):
#    python manage.py create_superuser --interactive
#
#    You'll be prompted:
#      Email [admin@codexvision.com]: 
#      First Name [System]: 
#      Last Name [Administrator]: 
#      Phone (optional): 
#      Password: 
#      Confirm password: 
#
# ─────────────────────────────────────────────────────────────────────────────
#
# 6. Force create (even if Vision Staff exists):
#    python manage.py create_superuser --force
#
# =============================================================================


# =============================================================================
# ENVIRONMENT VARIABLE APPROACH (More Secure)
# =============================================================================
#
# Instead of hardcoding defaults in the file, you can use environment variables:
#
# 1. Set environment variables:
#    export SUPERUSER_EMAIL="admin@codexvision.com"
#    export SUPERUSER_PASSWORD="SecureP@ss123"
#    export SUPERUSER_FIRST_NAME="System"
#    export SUPERUSER_LAST_NAME="Administrator"
#
# 2. Uncomment the os.getenv() lines in the DEFAULTS section above
#
# 3. Run the command (it will use env vars):
#    python manage.py create_superuser
#
# 4. On Render, set these as environment variables in the dashboard:
#    Settings → Environment → Add Environment Variable
#
# This keeps secrets out of your codebase entirely!
#
# =============================================================================