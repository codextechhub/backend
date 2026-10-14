"""No upload field reaches a caller who may read no mark, at any depth.

The four surfaces of a preview, rendered as a caller whose role has every
field of the sheet resource switched off, on the preview of a file that has a
replacement, a new score, a blank cell and a row with an issue.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile

from schools.vs_gradebook.services import importing, sheets
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from .import_base import workbook
from .sheet_base import SheetFixture

_SURFACE = "schools.vs_gradebook.serializers."


class SheetImportDeepPayloadTests(DeepPayloadChecks, SheetFixture):
    resources = frozenset({"gradebook.sheet"})
    covers = frozenset({
        _SURFACE + name for name in (
            "SheetImportPreviewSerializer", "SheetImportSummarySerializer",
            "SheetImportRowSerializer", "SheetImportCellSerializer",
        )
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.make_sheet(cls.jss1a, cls.maths, rows={cls.chiamaka: (18, 15, 46)})

    def samples(self):
        ctx = sheets.load(
            self.tenant, self.admin, self.jss1a.pk, self.maths.pk, self.first_term,
        )
        rows = [
            ["BFS/001", "Chiamaka", 18, 16, None],
            ["BFS/002", "Tunde", "ABS", 12, 50],
            ["BFS/999", "Nobody", 1, 1, 1],
        ]
        book = SimpleUploadedFile("scores.xlsx", workbook(rows))
        found = importing.preview(ctx, book)
        return [
            Sample(_SURFACE + "SheetImportPreviewSerializer", found),
            Sample(_SURFACE + "SheetImportSummarySerializer", found["summary"]),
            Sample(_SURFACE + "SheetImportRowSerializer", found["rows"][0]),
            Sample(_SURFACE + "SheetImportCellSerializer", found["rows"][0]["cells"][1]),
        ]
