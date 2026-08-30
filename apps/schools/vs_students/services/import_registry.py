"""Telling the import engine which permission key owns the students dataset.

The engine's ``HasImportBatchRBACPermission`` let a module's own import key
stand in for the generic ``import.batches.*`` ones, but only for bank
statements: the fallback was hard-coded to ``finance.bankaccount.import`` and a
BANK_STATEMENTS batch. So a school administrator holding
``school.students.import`` was refused the wizard however the key was granted,
and the failure looked like a seeding problem rather than a hard-coded one.

Registering the pair here rather than adding a second hard-coded branch keeps
the direction of the dependency pointing the way the platform prefers: the
domain app tells the engine, and the engine imports nothing.
"""
from __future__ import annotations

from ..constants import PERM_IMPORT


def register() -> None:
    from vs_import_data.permissions import register_dataset_import_key

    register_dataset_import_key("students", PERM_IMPORT)
