"""No gradebook field reaches a caller who may read none, at any depth.

Every declared surface of the gradebook resources is rendered as a caller whose role
has every field of that resource switched off, and the whole payload is walked.

The setup surfaces are rendered on the structure, scale and templates the
services serve. The sheet surfaces are rendered on the sheet and the moderation
queue the services build for a real returned sheet with an excused component.
The compile, broadsheet and hold surfaces are rendered on the payloads their
services build, in ``test_class_field_access_deep_payload``, and the release
list, report card and Results tab in ``test_card_field_access_deep_payload``.

Both shapes of school: Brightfield has two branches and Sunrise has one.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from ..constants import SheetStatus
from ..services import moderation, setup, sheets
from .base import GradebookFixture
from .sheet_base import SheetFixture

_SURFACE = "schools.vs_gradebook.serializers."


class GradebookDeepPayloadTests(DeepPayloadChecks, SheetFixture):
    resources = frozenset({
        "gradebook.setup", "gradebook.sheet", "gradebook.result",
    })
    covers = frozenset({
        _SURFACE + name for name in (
            "StructureSerializer", "ComponentSerializer", "StructureWriteSerializer",
            "ComponentWriteSerializer", "ScaleSerializer", "BandSerializer",
            "ScaleWriteSerializer", "BandWriteSerializer", "RemarkTemplatesSerializer",
            "RemarkTemplatesWriteSerializer", "SheetSerializer", "SheetRowSerializer",
            "SheetCellSerializer", "SheetStatsSerializer", "ResultRowSerializer",
            "SheetReturnedSerializer",
            "SheetProgressSerializer", "ModerationRowSerializer", "CellWriteSerializer",
            "ScoresWriteSerializer", "ReturnBodySerializer",
        )
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.structure = cls.junior_structure
        cls.scale = cls.junior_scale
        cls.moderate_seniors()
        cls.make_sheet(
            cls.jss1a, cls.maths, status=SheetStatus.RETURNED, returned_by=cls.okeke,
            returned_at=dt.datetime(2025, 10, 10, 9, 0, tzinfo=dt.timezone.utc),
            returned_note="Check the second test again.",
            rows={
                cls.chiamaka: (18, 15, 46), cls.tunde: ("EXC", 15, 46),
                cls.kelechi: (10, 10, 20),
            },
        )
        cls.make_sheet(
            cls.sss1a, cls.maths, status=SheetStatus.SUBMITTED, needs_moderation=True,
            rows={cls.zainab: (18, 15, 46), cls.femi: (5, 5, 10)},
        )

    def sheet_samples(self):
        ctx = sheets.load(self.tenant, self.admin, self.jss1a.pk, self.maths.pk, self.first_term)
        sheet = sheets.payload(ctx)
        queue = moderation.queue(
            self.tenant, self.okeke, self.first_term, None, sees_every_sheet=False,
        )
        cell = {"student": self.tunde.pk, "component": self.ca1.pk, "mark": 5, "marker": None}
        return [
            Sample(_SURFACE + "SheetSerializer", sheet),
            Sample(_SURFACE + "SheetRowSerializer", sheet["rows"][1]),
            Sample(_SURFACE + "SheetCellSerializer", sheet["rows"][0]["cells"][0]),
            Sample(_SURFACE + "SheetStatsSerializer", sheet["stats"]),
            Sample(_SURFACE + "SheetReturnedSerializer", sheet["returned"]),
            Sample(_SURFACE + "SheetProgressSerializer", sheet["progress"]),
            Sample(_SURFACE + "ModerationRowSerializer", queue["sheets"][0]),
            Sample(_SURFACE + "CellWriteSerializer", cell),
            Sample(_SURFACE + "ScoresWriteSerializer", {"cells": [cell]}),
            Sample(_SURFACE + "ReturnBodySerializer", {"note": "Recheck the examination."}),
        ]

    def samples(self):
        structure = setup.record_payload(setup.STRUCTURE, self.tenant, self.structure)
        scale = setup.record_payload(setup.SCALE, self.tenant, self.scale)
        templates = setup.templates_payload(self.tenant)
        template_body = {"templates": [{"band_min": 0, "text": "Come and see me."}]}
        write_structure = {
            "name": "Termly", "program": self.junior.pk, "branch": None,
            "components": self.components_body(),
        }
        write_scale = {
            "name": "Termly", "program": self.junior.pk, "branch": None,
            "bands": self.bands_body(),
        }

        result = {
            "student_id": self.chiamaka.pk, "name": "Chiamaka Nwosu",
            "overall_average": Decimal("76.25"), "position": 1,
            "ratings": {"Punctuality": 5, "Neatness": 4},
            "teacher_comment": "A joy to teach.", "principal_comment": "An excellent result.",
        }
        return [
            Sample(_SURFACE + "StructureSerializer", structure),
            Sample(_SURFACE + "ComponentSerializer", structure["components"][0]),
            Sample(_SURFACE + "StructureWriteSerializer", write_structure),
            Sample(_SURFACE + "ComponentWriteSerializer", write_structure["components"][0]),
            Sample(_SURFACE + "ScaleSerializer", scale),
            Sample(_SURFACE + "BandSerializer", scale["bands"][0]),
            Sample(_SURFACE + "ScaleWriteSerializer", write_scale),
            Sample(_SURFACE + "BandWriteSerializer", write_scale["bands"][0]),
            Sample(_SURFACE + "RemarkTemplatesSerializer", templates),
            Sample(_SURFACE + "RemarkTemplatesWriteSerializer", template_body),
            Sample(_SURFACE + "ResultRowSerializer", result),
            *self.sheet_samples(),
        ]


class SingleBranchSchoolDeepPayloadTests(GradebookFixture):
    """The same setup surfaces at a school with one branch, where no branch is named."""

    def test_a_school_with_one_branch_renders_the_setup_surfaces_without_a_branch_label(self):
        from ..serializers import StructureSerializer

        structure = self.make_structure(self.solo_program, tenant=self.solo)
        row = setup.record_payload(setup.STRUCTURE, self.solo, structure)
        data = StructureSerializer(row).data
        self.assertNotIn("scope_label", data)
        self.assertNotIn("is_shared", data)
        self.assertEqual(data["total"], 100)


# The class-to-card and amendment surfaces are rendered by cases of their own; importing them
# registers them with the registry's coverage check.
from . import test_amendment_field_access_deep_payload  # noqa: E402,F401
from . import test_card_field_access_deep_payload  # noqa: E402,F401
from . import test_class_field_access_deep_payload  # noqa: E402,F401
from . import test_sheet_import_field_access_deep_payload  # noqa: E402,F401
from . import test_cumulative_field_access_deep_payload  # noqa: E402,F401
from . import test_analytics_field_access_deep_payload  # noqa: E402,F401
