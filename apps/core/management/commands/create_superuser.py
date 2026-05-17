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

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.conf import settings

from vs_user.models import User
from vs_user.services.audit import log_auth_event
from vs_user.models import AuthEventLog
from vs_rbac.models import (
    PlatformRoleTemplate,
    PlatformUserRoleAssignment,
    PermissionModule,
    PermissionResource,
    Permission,
    PlatformRolePermission,
)


# Permission keys granted to the bootstrap xvs_super_admin role.
# The RBAC layer also short-circuits via is_vision_super_admin(), but listing
# the keys explicitly keeps the UI consistent and lets the role be cloned.
SUPERUSER_PERMISSION_KEYS = [
    # Permissions registry
    "platform.permissions.view",
    "platform.permissions.create",
    "platform.permissions.update",
    "platform.permissions.manage",
    "platform.permissions.delete",
    # Roles
    "platform.roles.view",
    "platform.roles.create",
    "platform.roles.update",
    "platform.roles.assign",
    "platform.roles.manage",
    "platform.roles.delete",
    "platform.roles.transfer",
    # Team
    "platform.team.view",
    "platform.team.create",
    "platform.team.update",
    "platform.team.delete",
    "platform.team.suspend",
    "platform.team.reactivate",
    # Schools
    "platform.schools.view",
    "platform.schools.create",
    "platform.schools.update",
    "platform.schools.delete",
    "platform.schools.manage",
    # Branches
    "platform.branches.view",
    "platform.branches.create",
    "platform.branches.update",
    "platform.branches.manage",
    # Audit
    "platform.audit.view",
    "platform.audit.export",
    "platform.audit.manage",
    # Dashboard
    "platform.dashboard.view",
]


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
            existing_count = User.objects.filter(user_type=User.UserType.VISION_STAFF).count()
        
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
            user_type=User.UserType.VISION_STAFF,
            status=User.Status.ACTIVE,
            is_active=True,
            is_staff=True,      # Django admin access
            is_superuser=True,  # Django admin superuser permissions
            school=None,        # Vision Staff have no school assignment
            branch=None,        # Vision Staff have no branch assignment
            invited_by=None,    # Self-created (bootstrap account)
        )
        
        # ── Assign XVS Super Admin role ───────────────────────────────────────
        try:
            super_admin_role = PlatformRoleTemplate.objects.get(id='xvs_super_admin')
            PlatformUserRoleAssignment.objects.get_or_create(
                user=user,
                role=super_admin_role,
                defaults={
                    'assignment_status': PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
                    'assigned_by': None,
                },
            )
        except PlatformRoleTemplate.DoesNotExist:
            self.stdout.write(self.style.WARNING(
                "  ⚠️  'xvs_super_admin' platform role not found."
            ))

        # ── Audit Log ─────────────────────────────────────────────────────────
        log_auth_event(
            actor=None,
            subject=user,
            school=None,
            event=AuthEventLog.Event.USER_CREATED,
            metadata={
                'bootstrap': True,
                'user_type': User.UserType.VISION_STAFF,
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
        """Bootstrap minimal permission system for superuser.

        Creates the platform module, all of its resources, and the permission
        rows referenced in view/serializer code (rbac_permission strings and
        FLS read_permissions/write_permissions). Without these rows, FLS
        always strips gated fields for non-super-admin Vision staff and the
        platform roles UI has nothing to grant.

        Also creates the xvs_super_admin role wired to a baseline set of
        permissions (managing the registry itself).

        Safe to re-run: every create uses get_or_create.
        """
        from vs_rbac.models import PermissionAction

        # Resource → action specs.
        # Action spec tuple: (action_name, description, is_restricted, sensitivity)
        # The full key becomes f"platform.{resource}.{action_name}".
        S = Permission.Sensitivity
        PLATFORM_RESOURCES: list[tuple[str, str, list[tuple[str, str, bool, str]]]] = [
            (
                'permissions',
                'Global permission registry management',
                [
                    ('view',   'View global permission registry',  False, S.NORMAL),
                    ('create', 'Add new permissions',              False, S.NORMAL),
                    ('update', 'Edit permission metadata',         False, S.NORMAL),
                    ('manage', 'Manage groups and dependencies',   True,  S.SENSITIVE),
                    ('delete', 'Delete permissions from registry', True,  S.NORMAL),
                ],
            ),
            (
                'roles',
                'Platform role template management',
                [
                    ('view',     'View platform roles',                       False, S.NORMAL),
                    ('create',   'Create new platform roles',                 False, S.NORMAL),
                    ('update',   'Edit platform role metadata',               False, S.NORMAL),
                    ('assign',   'Assign roles to users',                     True,  S.SENSITIVE),
                    ('manage',   'Full control over platform roles',          True,  S.SENSITIVE),
                    ('delete',   'Delete platform roles',                     True,  S.SENSITIVE),
                    ('transfer', 'Transfer Super Admin role to another user', True,  S.CRITICAL),
                ],
            ),
            (
                'team',
                'Vision staff team management',
                [
                    ('view',       'View Vision team members',         False, S.NORMAL),
                    ('create',     'Invite new Vision team members',   False, S.NORMAL),
                    ('update',     'Edit a team member profile',       False, S.NORMAL),
                    ('delete',     'Permanently remove a team member', True,  S.SENSITIVE),
                    ('suspend',    'Suspend a team member account',    True,  S.SENSITIVE),
                    ('reactivate', 'Reactivate a suspended account',   True,  S.SENSITIVE),
                ],
            ),
            (
                'schools',
                'Customer school management',
                [
                    ('view',   'View school list and detail',           False, S.NORMAL),
                    ('create', 'Onboard a new school',                  False, S.NORMAL),
                    ('update', 'Edit school info and settings',         False, S.NORMAL),
                    ('delete', 'Decommission a school record',          True,  S.SENSITIVE),
                    ('manage', 'Full school lifecycle administration',  True,  S.SENSITIVE),
                ],
            ),
            (
                'branches',
                'School branch management',
                [
                    ('view',   'View branches under a school',          False, S.NORMAL),
                    ('create', 'Add a new branch to a school',          False, S.NORMAL),
                    ('update', 'Edit branch details',                   False, S.NORMAL),
                    ('manage', 'Transition branch lifecycle',           True,  S.SENSITIVE),
                ],
            ),
            (
                'audit',
                'Audit and compliance',
                [
                    ('view',   'View audit events and entity trails',   False, S.NORMAL),
                    ('export', 'Export audit data to file',             True,  S.SENSITIVE),
                    ('manage', 'Create and manage compliance rules',    True,  S.SENSITIVE),
                ],
            ),
            (
                'dashboard',
                'Platform overview dashboard',
                [
                    ('view', 'View the platform overview dashboard',    False, S.NORMAL),
                ],
            ),
        ]

        try:
            module, _ = PermissionModule.objects.get_or_create(
                name='platform',
                defaults={'description': 'Vision platform administration', 'is_active': True},
            )

            for resource_name, resource_description, action_specs in PLATFORM_RESOURCES:
                resource, _ = PermissionResource.objects.get_or_create(
                    module=module,
                    name=resource_name,
                    defaults={'description': resource_description, 'is_active': True},
                )
                for action_name, desc, restricted, sensitivity in action_specs:
                    action = PermissionAction.objects.get(name=action_name)
                    perm_key = f"platform.{resource_name}.{action_name}"
                    Permission.objects.get_or_create(
                        key=perm_key,
                        defaults={
                            'module': module,
                            'resource': resource,
                            'action': action,
                            'description': desc,
                            'sensitivity_level': sensitivity,
                            'is_restricted': restricted,
                            'is_active': True,
                        },
                    )
            
            # Create or get xvs_super_admin role
            role, _ = PlatformRoleTemplate.objects.get_or_create(
                id='xvs_super_admin',
                defaults={
                    'name': 'XVS Super Admin',
                    'description': 'Full platform access',
                    'is_system_role': True,
                    'is_locked': True,
                    'status': PlatformRoleTemplate.Status.ACTIVE,
                }
            )
            
            # Wire permissions to role
            for perm_key in SUPERUSER_PERMISSION_KEYS:
                perm = Permission.objects.get(key=perm_key)
                PlatformRolePermission.objects.get_or_create(
                    role=role,
                    permission=perm,
                    defaults={'granted': True}
                )
        
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"⚠️  Permission bootstrap: {e}"))

    def _assign_role_to_existing(self, options):
        email = options['email'].strip().lower()

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"No user found with email: {email}"))
            return

        try:
            role = PlatformRoleTemplate.objects.get(id='xvs_super_admin')
        except PlatformRoleTemplate.DoesNotExist:
            self.stdout.write(self.style.ERROR(
                "'xvs_super_admin' platform role not found. Run seed_role_perms first."
            ))
            return

        assignment, created = PlatformUserRoleAssignment.objects.get_or_create(
            user=user,
            role=role,
            defaults={
                'assignment_status': PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
                'assigned_by': None,
            },
        )

        if not created and assignment.assignment_status == PlatformUserRoleAssignment.AssignmentStatus.REVOKED:
            assignment.assignment_status = PlatformUserRoleAssignment.AssignmentStatus.ACTIVE
            assignment.revoked_at = None
            assignment.revoked_by = None
            assignment.save(update_fields=['assignment_status', 'revoked_at', 'revoked_by', 'updated_at'])
            self.stdout.write(self.style.SUCCESS(f"  ✅ Vision Super Admin role re-activated for {user.email}"))
        elif created:
            self.stdout.write(self.style.SUCCESS(f"  ✅ Vision Super Admin role assigned to {user.email}"))
        else:
            self.stdout.write(self.style.WARNING(f"  ℹ️  {user.email} already has Vision Super Admin role (active)."))

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