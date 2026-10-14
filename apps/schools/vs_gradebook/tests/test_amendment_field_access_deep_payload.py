"""No amendment field reaches a caller who may read none, at any depth.

The surfaces of the amendment screens, rendered as a caller whose role has every
field of the resource switched off, and the whole payload walked at every depth.
Each is rendered on the payload its service builds for a real correction to
JSS1 A once it is approved: the list row with its before and after lines, the
history entry on the Results tab, the drawer, and the bodies the screens send.

The amendment resource holds the reason and the decision note. The lines and the
drawer are sheet surfaces, because the cells in them are marks and follow the
sheet's switches.
"""
from __future__ import annotations

from schools.vs_gradebook.services import amendments
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from .amend_base import AmendmentFixture

_SURFACE = "schools.vs_gradebook.serializers."


class AmendmentDeepPayloadTests(DeepPayloadChecks, AmendmentFixture):
    resources = frozenset({"gradebook.amendment", "gradebook.sheet"})
    covers = frozenset({
        _SURFACE + name for name in (
            "AmendmentRowSerializer", "AmendmentHistorySerializer",
            "AmendmentRaiseBodySerializer", "AmendmentDecisionBodySerializer",
            "AmendmentFormSerializer", "AmendmentLineSerializer",
            "AmendmentCellWriteSerializer", "AmendmentCellsBodySerializer",
        )
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()
        target = amendments.open_target(
            cls.tenant, cls.amender, cls.jss1a.pk, cls.first_term, cls.kelechi.pk, cls.maths.pk,
        )
        cls.amendment = amendments.raise_amendment(
            target, "Marked the paper wrongly.",
            [{"component": cls.exam.pk, "mark": 50, "marker": None}],
        )
        found = amendments.open_for_decision(cls.tenant, cls.approver, cls.amendment.pk)
        amendments.approve(cls.tenant, cls.approver, found, "Checked the script.")

    def samples(self):
        decided = amendments.one_payload(self.tenant, self.amendment)
        entry = amendments.history_entries(self.tenant, self.kelechi)[0]["amendment"]
        target = amendments.open_target(
            self.tenant, self.approver, self.jss1a.pk, self.first_term, self.kelechi.pk,
            self.maths.pk,
        )
        cell = {"component": self.exam.pk, "mark": 50, "marker": None}
        return [
            Sample(_SURFACE + "AmendmentRowSerializer", decided),
            Sample(_SURFACE + "AmendmentHistorySerializer", entry),
            Sample(_SURFACE + "AmendmentRaiseBodySerializer", {
                "term": self.first_term.pk, "class": self.jss1a.pk, "student": self.kelechi.pk,
                "subject": self.maths.pk, "amendment_reason": "Marked the paper wrongly.",
            }),
            Sample(_SURFACE + "AmendmentDecisionBodySerializer", {
                "decision_note": "Checked the script.",
            }),
            Sample(_SURFACE + "AmendmentFormSerializer", amendments.form(target)),
            Sample(_SURFACE + "AmendmentLineSerializer", decided["after"]),
            Sample(_SURFACE + "AmendmentCellWriteSerializer", cell),
            Sample(_SURFACE + "AmendmentCellsBodySerializer", {"cells": [cell]}),
        ]
