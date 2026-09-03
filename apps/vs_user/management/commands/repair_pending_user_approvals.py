from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_tenants.models import Tenant
from vs_tenants.references import find_tenant
from vs_user.email_normalization import normalize_email
from vs_user.models import PlatformStaffProfile, User
from vs_user.services.user import UserCreationService
from vs_workflow.models import WorkflowInstance
from vs_workflow.services.submission import submit_for_approval


class Command(BaseCommand):
    help = (
        "Submit orphaned CX users left in PENDING_APPROVAL without a workflow "
        "instance. Safe to rerun."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            help="Repair only this user email (recommended on staging).",
        )
        parser.add_argument(
            "--tenant_id",
            metavar="TENANT",
            help=(
                "Narrow --email to one tenant, by numeric id or slug. An "
                "address can be a login at several tenants, so it no longer "
                "identifies one account on its own."
            ),
        )

    def handle(self, *args, **options):
        users = User.objects.filter(
            tenant__kind=Tenant.Kind.PLATFORM,
            status=User.Status.PENDING_APPROVAL,
        ).select_related("tenant", "invited_by")
        # Filters and iterates rather than taking .get() or .first(), so an address
        # held at two tenants repairs both. That is safe, the work being idempotent,
        # but it is not always what the operator asked for, so --tenant_id narrows
        # it. The tenant-kind filter above admits only platform users.
        if options.get("tenant_id"):
            tenant = find_tenant(options["tenant_id"])
            if tenant is None:
                raise CommandError(
                    f"No tenant found for --tenant_id {options['tenant_id']!r} (id or slug)."
                )
            users = users.filter(tenant=tenant)
        if options.get("email"):
            users = users.filter(email=normalize_email(options["email"]))

        content_type = ContentType.objects.get_for_model(User)
        repaired = 0
        skipped = 0

        for candidate in users.iterator():
            if WorkflowInstance.objects.filter(
                document_content_type=content_type,
                document_object_id=str(candidate.pk),
                document_type=User.workflow_document_type,
            ).exists():
                skipped += 1
                continue
            if candidate.invited_by_id is None:
                raise CommandError(
                    f"Cannot repair {candidate.email}: the original inviter is missing."
                )

            with transaction.atomic():
                user = (
                    User.objects.select_for_update()
                    # invited_by is nullable; joining it here would put the
                    # nullable side under FOR UPDATE, which PostgreSQL rejects.
                    .select_related("tenant")
                    .get(pk=candidate.pk)
                )
                profile, _ = PlatformStaffProfile.objects.get_or_create(user=user)
                if not profile.employee_id:
                    profile.employee_id = UserCreationService._next_employee_id(user.tenant)
                    profile.save(update_fields=["employee_id", "updated_at"])

                instance = submit_for_approval(
                    document=user,
                    requested_by=user.invited_by,
                )
                repaired += 1
                self.stdout.write(
                    f"Repaired {user.email}: workflow {instance.pk} is {instance.status}."
                )

        self.stdout.write(self.style.SUCCESS(
            f"Done. Repaired {repaired}; skipped {skipped} already submitted."
        ))
