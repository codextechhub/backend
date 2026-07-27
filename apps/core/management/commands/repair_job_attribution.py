"""Re-attribute invitation-email jobs that were owned by the invitee.

Invitation jobs used to be queued with the *invited* user as ``_job_owner_id``
instead of the admin who triggered the send. Two things followed for every
already-invited account: a queue row on "View Queues" for work they never
started, and a ``task.completed`` bell notification about an email an admin had
sent *to* them — visible the moment they activated.

The forward fix lives in ``core.tasks_base`` and the invitation call sites. This
command cleans up the rows those older sends left behind:

  * the job's owner moves to ``UserInvitation.invited_by`` (or to NULL — a
    system row — when the inviter is no longer recorded),
  * ``notify_owner`` is turned off, matching how these jobs are queued now,
  * the mis-delivered ``task.completed`` / ``task.failed`` notifications are
    deleted from the invitee's feed.

Only invitation jobs are touched: their label carries the invitee's email, so
"the owner is the subject" is decidable. Password-reset jobs are left alone —
a SELF reset is legitimately owned by the same user, and nothing on the row
distinguishes it from an admin-triggered one after the fact.

Dry-run by default; pass ``--commit`` to write. Idempotent — a repaired row no
longer matches the owner-is-subject test.

    python manage.py repair_job_attribution
    python manage.py repair_job_attribution --commit
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import BackgroundJob

INVITE_TASK = "vs_user.send_invitation_email_task"
NOTIFY_KEYS = ("task.completed", "task.failed")


class Command(BaseCommand):
    help = "Re-attribute invitation-email jobs owned by the invitee to the inviting admin."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true",
                            help="Actually write the changes (default: dry-run).")

    def handle(self, *args, **opts):
        from vs_notifications.constants import ChannelChoices
        from vs_notifications.models import Notification
        from vs_user.models import UserInvitation

        commit = opts.get("commit")
        jobs = (
            BackgroundJob.objects
            .filter(task_name=INVITE_TASK, owner__isnull=False)
            .select_related("owner")
            .order_by("created_at")
        )

        repaired = 0
        notes_removed = 0
        for job in jobs:
            # The label names the invitee; owner == invitee is the mis-attribution.
            if not job.owner.email or job.owner.email not in job.label:
                continue

            invitation = UserInvitation.objects.filter(user=job.owner).first()
            new_owner_id = invitation.invited_by_id if invitation else None
            if new_owner_id == job.owner_id:
                # System provisioning records invited_by as the invitee itself;
                # there is no actor to hand it to, so it becomes a system row.
                new_owner_id = None
            stale = Notification.objects.filter(
                recipient=job.owner,
                channel=ChannelChoices.IN_APP,
                event_type__key__in=NOTIFY_KEYS,
                body__contains=job.label,
            )
            stale_count = stale.count()

            self.stdout.write(
                f"  {job.celery_task_id}: owner {job.owner.email} → "
                f"{new_owner_id or 'system (NULL)'}; drops {stale_count} notification(s)"
            )
            repaired += 1
            notes_removed += stale_count

            if commit:
                with transaction.atomic():
                    stale.delete()
                    job.owner_id = new_owner_id
                    job.notify_owner = False
                    job.save(update_fields=["owner", "notify_owner"])

        verb = "Repaired" if commit else "Would repair"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {repaired} job(s) and removed {notes_removed} mis-delivered notification(s)."
        ))
        if repaired and not commit:
            self.stdout.write("Re-run with --commit to apply.")
