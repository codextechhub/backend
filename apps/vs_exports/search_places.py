"""The Export Centre screens the search bar can route to.

Exports answer to the ``data_export`` capability (:mod:`vs_rbac.capability_map`).
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

from .constants import ExportPermission

CAPABILITY = "data_export"

register(
    Place(
        kind=ACTION, key="exports.new", label="New export",
        description="Choose a dataset, its columns and filters, and download it.",
        keywords=("download", "spreadsheet", "report"),
        screen="exports.new", permission=ExportPermission.CATALOGUE_VIEW,
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="exports.saved", label="Saved exports",
        description="Exports saved to run again.", keywords=("download",),
        screen="exports.saved",
        permission=(ExportPermission.DEFINITION_VIEW, ExportPermission.DEFINITION_CREATE),
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="exports.files", label="Export files",
        description="Every export that has run, and its file.", keywords=("download",),
        screen="exports.files", permission=ExportPermission.RUN_VIEW,
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="exports.schedules", label="Scheduled exports",
        description="Exports that run on a schedule.", keywords=("recurring",),
        screen="exports.schedules",
        permission=(ExportPermission.SCHEDULE_VIEW, ExportPermission.SCHEDULE_CREATE),
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="exports.activity", label="Export activity",
        description="Who exported what, and when.", keywords=("audit",),
        screen="exports.activity", permission=ExportPermission.ACTIVITY_VIEW,
        capability=CAPABILITY,
    ),
)
