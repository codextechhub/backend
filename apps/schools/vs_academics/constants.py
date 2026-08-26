"""Names this module's views, services and seeder must agree on.

Kept in one place so a typo cannot make a view demand a key the seeder never
registers, which fails as a 403 nobody can act on rather than as an error.
"""
from __future__ import annotations

# ── Permission keys ────────────────────────────────────────────────────────
# Seeded by core.management.commands.seed_school_permissions.
PERM_SESSION_VIEW = "academics.session.view"
PERM_SESSION_CREATE = "academics.session.create"
PERM_SESSION_UPDATE = "academics.session.update"
PERM_SESSION_MANAGE = "academics.session.manage"

PERM_STRUCTURE_VIEW = "academics.structure.view"
PERM_STRUCTURE_CREATE = "academics.structure.create"
PERM_STRUCTURE_UPDATE = "academics.structure.update"
PERM_STRUCTURE_MANAGE = "academics.structure.manage"

PERM_CLASSES_VIEW = "academics.classes.view"
PERM_CLASSES_CREATE = "academics.classes.create"
PERM_CLASSES_UPDATE = "academics.classes.update"
PERM_CLASSES_MANAGE = "academics.classes.manage"

PERM_SUBJECT_VIEW = "academics.subject.view"
PERM_SUBJECT_CREATE = "academics.subject.create"
PERM_SUBJECT_UPDATE = "academics.subject.update"
PERM_SUBJECT_MANAGE = "academics.subject.manage"
