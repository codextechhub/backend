"""
Management command: seed_organogram
===================================
Backfills the CX organogram for an existing database:

  1. Builds a starter OrgNode tree (DIVISION → DEPARTMENT → TEAM).
  2. Creates the Positions (seats) for that tree, with solid reporting lines.
  3. Ensures every CX_STAFF user has a PlatformStaffProfile.
  4. Auto-assigns CX_STAFF users who have no current primary seat — filling the
     management seats first (so line-manager / department-head derivation works)
     then round-robin across the team seats.

Safe to re-run: nodes/positions use get_or_create (by code), profiles use
get_or_create, and only users WITHOUT a current primary assignment are slotted
in — so an extra run just fills new gaps, it never reshuffles people.

Usage
-----
    python manage.py seed_organogram
    python manage.py seed_organogram --skip-assign     # tree + profiles only
    python manage.py seed_organogram --reassign        # also move already-seated users
    python manage.py seed_organogram --dry-run         # show what would change, roll back
"""

from __future__ import annotations

from itertools import cycle

from django.core.management.base import BaseCommand
from django.db import transaction

from vs_user.models import User, OrgNode, Position, PlatformStaffProfile
from vs_user.services.organogram import OrganogramService


# ── Starter org tree ──────────────────────────────────────────────────────────
# (code, name, kind, parent_code)  — ordered so parents come before children.
ORG_NODES = [
    ("TECH",     "Technology",  OrgNode.Kind.DIVISION,   None),
    ("ENG",      "Engineering", OrgNode.Kind.DEPARTMENT, "TECH"),
    ("PROD",     "Product",     OrgNode.Kind.DEPARTMENT, "TECH"),
    ("BACKEND",  "Backend",     OrgNode.Kind.TEAM,       "ENG"),
    ("FRONTEND", "Frontend",    OrgNode.Kind.TEAM,       "ENG"),
    ("QA",       "QA",          OrgNode.Kind.TEAM,       "ENG"),
    ("DESIGN",   "Design",      OrgNode.Kind.TEAM,       "PROD"),
]

# (code, title, org_node_code, reports_to_code, headcount) — parents first.
POSITIONS = [
    ("VP-TECH",  "VP, Technology",      "TECH",     None,       1),
    ("ENG-MGR",  "Engineering Manager", "ENG",      "VP-TECH",  1),
    ("PROD-MGR", "Product Manager",     "PROD",     "VP-TECH",  1),
    ("BE-ENG",   "Backend Engineer",    "BACKEND",  "ENG-MGR",  50),
    ("FE-ENG",   "Frontend Engineer",   "FRONTEND", "ENG-MGR",  50),
    ("QA-ENG",   "QA Engineer",         "QA",       "ENG-MGR",  50),
    ("DESIGNER", "Product Designer",    "DESIGN",   "PROD-MGR", 50),
]

# Each node's heading position (set after positions exist).
NODE_HEADS = {
    "TECH": "VP-TECH",
    "ENG":  "ENG-MGR",
    "PROD": "PROD-MGR",
}

# Seats people get slotted into, management first then team seats (round-robin).
MANAGEMENT_SEATS = ["VP-TECH", "ENG-MGR", "PROD-MGR"]
TEAM_SEATS       = ["BE-ENG", "FE-ENG", "QA-ENG", "DESIGNER"]


class Command(BaseCommand):
    help = "Backfill the CX organogram (tree + positions + profiles + assignments) for existing data."

    def add_arguments(self, parser):
        parser.add_argument("--skip-assign", action="store_true",
                            help="Build the tree and profiles but do not assign anyone to a seat.")
        parser.add_argument("--reassign", action="store_true",
                            help="Also (re)assign users who already hold a current primary seat.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would change, then roll everything back.")

    def handle(self, *args, **opts):
        skip_assign = opts["skip_assign"]
        reassign    = opts["reassign"]
        dry_run     = opts["dry_run"]

        class _Rollback(Exception):
            pass

        stats = {"nodes": 0, "positions": 0, "heads": 0, "profiles": 0, "assignments": 0}

        try:
            with transaction.atomic():
                nodes = self._seed_nodes(stats)
                positions = self._seed_positions(nodes, stats)
                self._set_heads(nodes, positions, stats)
                self._backfill_profiles(stats)
                if not skip_assign:
                    self._assign_users(positions, reassign, stats)

                if dry_run:
                    raise _Rollback()
        except _Rollback:
            self.stdout.write(self.style.WARNING("\nDRY RUN — all changes rolled back."))

        self.stdout.write(self.style.SUCCESS(
            "\nDone. nodes=%(nodes)d positions=%(positions)d heads=%(heads)d "
            "profiles=%(profiles)d assignments=%(assignments)d" % stats
        ))

    # ── Steps ──────────────────────────────────────────────────────────────────

    def _seed_nodes(self, stats) -> dict:
        nodes: dict[str, OrgNode] = {}
        for code, name, kind, parent_code in ORG_NODES:
            parent = nodes.get(parent_code) if parent_code else None
            # OrgNode.save() prefixes the code by tier (DV-/DT-/TM-), so look up
            # by the prefixed code — matching on the bare code would never find
            # an existing node and would duplicate-key on every re-run.
            lookup_code = f"{OrgNode._KIND_PREFIX.get(kind, '')}{code}"
            node, created = OrgNode.objects.get_or_create(
                code=lookup_code,
                defaults={"name": name, "kind": kind, "parent": parent},
            )
            nodes[code] = node
            if created:
                stats["nodes"] += 1
                self.stdout.write(f"  + OrgNode {kind:<10} {code} ({name})")
        return nodes

    def _seed_positions(self, nodes, stats) -> dict:
        positions: dict[str, Position] = {}
        for code, title, node_code, reports_code, headcount in POSITIONS:
            reports_to = positions.get(reports_code) if reports_code else None
            pos, created = Position.objects.get_or_create(
                code=code,
                defaults={
                    "title": title,
                    "org_node": nodes[node_code],
                    "reports_to": reports_to,
                    "headcount": headcount,
                },
            )
            positions[code] = pos
            if created:
                stats["positions"] += 1
                self.stdout.write(f"  + Position {code} ({title})")
        return positions

    def _set_heads(self, nodes, positions, stats):
        for node_code, pos_code in NODE_HEADS.items():
            node = nodes[node_code]
            head = positions[pos_code]
            if node.head_position_id != head.id:
                node.head_position = head
                node.save(update_fields=["head_position", "updated_at"])
                stats["heads"] += 1

    def _backfill_profiles(self, stats):
        cx_users = User.objects.filter(user_type=User.UserType.CX_STAFF)
        for user in cx_users:
            _, created = PlatformStaffProfile.objects.get_or_create(user=user)
            if created:
                stats["profiles"] += 1
                self.stdout.write(f"  + Profile for {user.email}")

    def _assign_users(self, positions, reassign, stats):
        # Order users so any super admin lands at the top seat, then by id.
        from vs_rbac.models import TenantUserRoleAssignment
        admin_ids = set(
            TenantUserRoleAssignment.objects
            .filter(role__key="xvs_super_admin", role__tenant__kind="PLATFORM")
            .values_list("user_id", flat=True)
        )
        cx_users = list(User.objects.filter(user_type=User.UserType.CX_STAFF).order_by("id"))
        cx_users.sort(key=lambda u: (u.id not in admin_ids, u.id))

        # Skip anyone who already has a current primary seat (unless --reassign).
        def has_seat(user) -> bool:
            return user.position_assignments.filter(is_primary=True, end_date__isnull=True).exists()

        pending = [u for u in cx_users if reassign or not has_seat(u)]

        # Management seats only need filling if currently vacant.
        def seat_filled(pos) -> bool:
            return pos.assignments.filter(end_date__isnull=True).exists()

        plan = [positions[c] for c in MANAGEMENT_SEATS if not seat_filled(positions[c])]
        team_cycle = cycle(positions[c] for c in TEAM_SEATS)

        for user in pending:
            seat = plan.pop(0) if plan else next(team_cycle)
            OrganogramService.assign_position(user=user, position=seat, assigned_by=None)
            stats["assignments"] += 1
            self.stdout.write(f"  → {user.email} assigned to {seat.code}")
