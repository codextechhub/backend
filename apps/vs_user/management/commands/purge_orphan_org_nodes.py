"""
Management command: purge_orphan_org_nodes   (ONE-OFF — delete after running)
============================================================================
Cleans up the org nodes stranded by the old `OrgNode.parent = SET_NULL`
behaviour: deleting a Division silently set its Departments' parent to NULL
(and deleting a Department did the same to its Teams), producing parentless
Departments/Teams. That state is illegal by OrgNode.clean() — such rows can
neither be edited (the serializer re-runs clean()) nor deleted (their Positions
are PROTECTed), and they vanish from every department/division roll-up.

`parent` is PROTECT from migration vs_user.0004 onward, so no NEW orphans can
appear. This command removes the ones already in the database.

What it deletes, per orphan (a non-Division node with no parent) — including
its whole subtree, deepest node first:

  1. PositionAssignment rows of every Position in the subtree (tenure history)
  2. those Positions           (PROTECT is why the node would not delete)
  3. the OrgNode rows themselves

Side effects it reports but does not block on (all SET_NULL / CASCADE):
staff profiles lose their `position` link, workflow stages lose
`organogram_position`, matrix (dotted) lines cascade away, and positions
outside the subtree that reported INTO a deleted seat get `reports_to = NULL`.

Safety: dry-run by default — nothing is written without --apply. Orphans
holding a CURRENT (open) assignment are skipped and listed, because deleting
them detaches a real person from their seat; pass --force to delete those too.

Usage
-----
    python manage.py purge_orphan_org_nodes                 # report only
    python manage.py purge_orphan_org_nodes --apply         # delete the safe ones
    python manage.py purge_orphan_org_nodes --apply --force # also seated ones
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from vs_user.models import (
    MatrixReport,
    OrgNode,
    Position,
    PositionAssignment,
    PlatformStaffProfile,
)


# How many seat holders to name per orphan before collapsing to a count.
SEAT_SAMPLE = 6


def _holder(user) -> str:
    """Readable label for a seat holder (full_name is a property, may be blank)."""
    return user.full_name or user.email


class Command(BaseCommand):
    help = 'One-off cleanup: delete org nodes orphaned by the old SET_NULL parent behaviour.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually delete. Without it the command only reports.',
        )
        parser.add_argument(
            '--force', action='store_true',
            help='Also delete orphans whose seats have a current (open) assignment.',
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _subtree(root: OrgNode) -> list[OrgNode]:
        """`root` plus every descendant, ordered deepest-first (delete order)."""
        levels: list[list[OrgNode]] = [[root]]
        while levels[-1]:
            ids = [n.pk for n in levels[-1]]
            levels.append(list(OrgNode.objects.filter(parent_id__in=ids).order_by('pk')))
        return [node for level in reversed(levels) for node in level]

    def handle(self, *args, **options):
        apply_changes = options['apply']
        force = options['force']

        orphans = list(
            OrgNode.objects
            .exclude(kind=OrgNode.Kind.DIVISION)
            .filter(parent__isnull=True)
            .order_by('kind', 'name')
        )
        if not orphans:
            self.stdout.write(self.style.SUCCESS('No orphaned org nodes. Nothing to do.'))
            return

        self.stdout.write(
            self.style.WARNING(f'Found {len(orphans)} orphaned org node(s).')
        )

        planned: list[OrgNode] = []       # nodes to delete, deepest-first
        skipped: list[tuple[OrgNode, list[str]]] = []

        for orphan in orphans:
            nodes = self._subtree(orphan)
            node_ids = [n.pk for n in nodes]
            positions = list(Position.objects.filter(org_node_id__in=node_ids))
            position_ids = [p.pk for p in positions]

            current = list(
                PositionAssignment.objects
                .filter(position_id__in=position_ids, end_date__isnull=True)
                .select_related('user', 'position')
            )
            past_count = PositionAssignment.objects.filter(
                position_id__in=position_ids, end_date__isnull=False,
            ).count()

            self.stdout.write('')
            self.stdout.write(
                f'  {orphan.kind} {orphan.code} "{orphan.name}" (id={orphan.pk})'
            )
            for node in nodes:
                if node.pk != orphan.pk:
                    self.stdout.write(f'      └─ {node.kind} {node.code} "{node.name}" (id={node.pk})')
            self.stdout.write(
                f'      positions={len(positions)}  current assignments={len(current)}  '
                f'past assignments={past_count}'
            )
            if positions:
                self.stdout.write(
                    '      seats: ' + ', '.join(f'{p.code} ({p.title})' for p in positions)
                )
            # A big department can hold dozens of seats — show a sample, count
            # the rest, so the report stays readable.
            for assignment in current[:SEAT_SAMPLE]:
                self.stdout.write(self.style.WARNING(
                    f'      SEATED: {_holder(assignment.user)} '
                    f'in {assignment.position.code} since {assignment.start_date}'
                ))
            if len(current) > SEAT_SAMPLE:
                self.stdout.write(self.style.WARNING(
                    f'      SEATED: … and {len(current) - SEAT_SAMPLE} more'
                ))

            # Side effects worth naming before anything is written.
            if position_ids:
                profiles = PlatformStaffProfile.objects.filter(position_id__in=position_ids).count()
                inbound = Position.objects.filter(
                    reports_to_id__in=position_ids,
                ).exclude(pk__in=position_ids).count()
                matrix = MatrixReport.objects.filter(position_id__in=position_ids).count()
                matrix += MatrixReport.objects.filter(reports_to_id__in=position_ids).count()
                heads = OrgNode.objects.filter(
                    head_position_id__in=position_ids,
                ).exclude(pk__in=node_ids).count()
                if profiles or inbound or matrix or heads:
                    self.stdout.write(
                        f'      side effects: staff profiles unlinked={profiles}  '
                        f'outside seats losing their manager={inbound}  '
                        f'matrix lines removed={matrix}  '
                        f'other nodes losing their head seat={heads}'
                    )

            if current and not force:
                skipped.append((orphan, [
                    f'{_holder(a.user)} in {a.position.code}' for a in current
                ]))
                self.stdout.write(self.style.WARNING(
                    '      SKIPPED — currently seated. Re-run with --force to delete anyway.'
                ))
                continue

            planned.extend(nodes)

        self.stdout.write('')
        if not planned:
            self.stdout.write(self.style.WARNING(
                f'Nothing deletable: all {len(skipped)} orphan(s) hold current assignments. '
                'Close those assignments, or re-run with --force.'
            ))
            return

        planned_ids = [n.pk for n in planned]
        position_ids = list(
            Position.objects.filter(org_node_id__in=planned_ids).values_list('pk', flat=True)
        )
        assignment_count = PositionAssignment.objects.filter(position_id__in=position_ids).count()

        summary = (
            f'{len(planned)} org node(s), {len(position_ids)} position(s), '
            f'{assignment_count} assignment row(s)'
        )
        if not apply_changes:
            self.stdout.write(self.style.WARNING(f'DRY RUN — would delete {summary}.'))
            self.stdout.write('Re-run with --apply to delete.')
            return

        with transaction.atomic():
            PositionAssignment.objects.filter(position_id__in=position_ids).delete()
            Position.objects.filter(pk__in=position_ids).delete()
            # Deepest-first: `parent` is PROTECT now, so a parent cannot go
            # before its children.
            for node in planned:
                node.delete()

        self.stdout.write(self.style.SUCCESS(f'Deleted {summary}.'))
        if skipped:
            self.stdout.write(self.style.WARNING(
                f'{len(skipped)} orphan(s) left in place because they are currently seated:'
            ))
            for node, holders in skipped:
                self.stdout.write(f'  {node.code} "{node.name}" — ' + '; '.join(holders))
