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
PERM_SESSION_ACTIVATE = "academics.session.activate"
PERM_SESSION_ARCHIVE = "academics.session.archive"
PERM_SESSION_DELETE = "academics.session.delete"

PERM_STRUCTURE_VIEW = "academics.structure.view"
PERM_STRUCTURE_CREATE = "academics.structure.create"
PERM_STRUCTURE_UPDATE = "academics.structure.update"
PERM_STRUCTURE_ARCHIVE = "academics.structure.archive"
PERM_STRUCTURE_REACTIVATE = "academics.structure.reactivate"
PERM_STRUCTURE_IMPORT = "academics.structure.import"

PERM_CLASSES_VIEW = "academics.classes.view"
PERM_CLASSES_CREATE = "academics.classes.create"
PERM_CLASSES_UPDATE = "academics.classes.update"
PERM_CLASSES_ARCHIVE = "academics.classes.archive"
PERM_CLASSES_REACTIVATE = "academics.classes.reactivate"

PERM_SUBJECT_VIEW = "academics.subject.view"
PERM_SUBJECT_CREATE = "academics.subject.create"
PERM_SUBJECT_UPDATE = "academics.subject.update"
PERM_SUBJECT_ARCHIVE = "academics.subject.archive"
PERM_SUBJECT_REACTIVATE = "academics.subject.reactivate"
