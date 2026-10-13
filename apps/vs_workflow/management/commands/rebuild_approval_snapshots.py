"""Retake the approval snapshot of every request still in front of its approvers.

An approval request carries a snapshot of its document, taken when the request
was sent: the card an approver sees and the decision layout beneath it. The
snapshot marks each governed value with the Field Access key it came from, so
that an approver whose roles cannot read the field is not shown the value. A
snapshot taken before those markers existed carries none, and shows every value
to every approver until it is taken again.

This command takes it again, through the document type's own handler, for every
open request (anything not yet approved, rejected, withdrawn or cancelled,
returned requests included). A closed request is a record of what was decided
and is left as it was.

It is safe to run again: a request whose rebuilt snapshot equals the stored one
is not written, a request whose document can no longer be loaded or whose type
has no handler keeps its snapshot, and a handler that fails on one request
leaves that request as it was and does not stop the rest. Each rewritten
request is logged under ``vs_workflow.snapshots``.

    manage.py rebuild_approval_snapshots --dry-run
    manage.py rebuild_approval_snapshots
    manage.py rebuild_approval_snapshots --tenant corona --document-type finance.refund
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger("vs_workflow.snapshots")


class Command(BaseCommand):
    help = "Rebuild the summary and details snapshot of every open approval request."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be rewritten and write nothing.",
        )
        parser.add_argument("--tenant", default="", help="Limit to one tenant slug.")
        parser.add_argument("--document-type", default="", help="Limit to one document type.")

    def handle(self, *args, **options):
        from vs_workflow.exceptions import UnknownDocumentTypeError
        from vs_workflow.handlers.registry import get_handler
        from vs_workflow.models import WorkflowInstance
        from vs_workflow.services.submission import current_document_snapshot

        dry_run = options["dry_run"]
        open_requests = WorkflowInstance.all_objects.active().select_related(
            "document_content_type",
        )
        if options["tenant"]:
            open_requests = open_requests.filter(tenant__slug=options["tenant"].strip().lower())
        if options["document_type"]:
            open_requests = open_requests.filter(document_type=options["document_type"].strip())

        counts = {"rewritten": 0, "unchanged": 0, "skipped": 0, "failed": 0}
        for instance in open_requests.order_by("created_at").iterator():
            try:
                handler = get_handler(instance.document_type)
            except UnknownDocumentTypeError:
                counts["skipped"] += 1
                continue
            try:
                snapshot = current_document_snapshot(instance, handler)
            except Exception:
                logger.exception("Could not rebuild the snapshot of approval request %s.", instance.pk)
                counts["failed"] += 1
                continue
            if snapshot is None:
                counts["skipped"] += 1
                continue
            summary, details = snapshot
            if summary == instance.document_summary and details == instance.document_details:
                counts["unchanged"] += 1
                continue
            counts["rewritten"] += 1
            if dry_run:
                continue
            WorkflowInstance.all_objects.filter(pk=instance.pk).update(
                document_summary=summary, document_details=details,
            )
            logger.info(
                "Rebuilt the approval snapshot of request %s (%s).",
                instance.pk, instance.document_type,
            )

        verb = "Would rewrite" if dry_run else "Rewrote"
        self.stdout.write(
            f"{verb} {counts['rewritten']} open approval request(s); "
            f"{counts['unchanged']} already current, {counts['skipped']} skipped "
            f"(no handler or document), {counts['failed']} failed."
        )
