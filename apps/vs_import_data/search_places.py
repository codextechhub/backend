"""The data import screens the search bar can route to.

Imports answer to the ``bulk_import`` capability (:mod:`vs_rbac.capability_map`).
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

from .constants import ImportPermission

CAPABILITY = "bulk_import"

register(
    Place(
        kind=ACTION, key="imports.batches", label="Import batches",
        description="Spreadsheets uploaded, what each loaded and what it refused.",
        keywords=("upload", "spreadsheet", "bulk upload"),
        screen="imports.batches", permission=ImportPermission.BATCH_VIEW,
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="imports.templates", label="Import templates",
        description="The spreadsheet layouts each kind of record is loaded from.",
        keywords=("upload", "spreadsheet", "datasets"),
        screen="imports.templates", permission=ImportPermission.TEMPLATE_VIEW,
        capability=CAPABILITY,
    ),
)
