"""No release-list, report-card or Results-tab field reaches a caller who may read none, at any depth.

The surfaces of the release list, the printed report card and a student's
Results tab, rendered as a caller whose role has every field of the resource
switched off, and the whole payload walked at every depth. Each is rendered on
the payload its service builds for JSS1 A once it is published: Tunde's card
with ratings, class figures and a position, the release list with a guardian, a
sent email and a held student, and the Results tab of the held student with a
hold in its history.

A label built from a hidden value (the card's "Position in class" line) is not
a key and cannot be found by the walk; ``test_card_field_access`` proves those
lines gone.
"""
from __future__ import annotations

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from ..services import classbook, reportcards, student_results
from .card_base import CardFixture

_SURFACE = "schools.vs_gradebook.serializers."


class CardDeepPayloadTests(DeepPayloadChecks, CardFixture):
    resources = frozenset({"gradebook.result", "gradebook.sheet", "gradebook.hold"})
    covers = frozenset({
        _SURFACE + name for name in (
            "ReleaseRowSerializer", "ReportCardSerializer", "CardSubjectSerializer",
            "ResultSubjectSerializer", "ResultTermSerializer", "StudentResultsSerializer",
            "ResultHistorySerializer",
        )
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a(held=[cls.amina])

    def samples(self):
        ctx = classbook.ClassContext(
            self.tenant, self.releaser, self.jss1a, self.lekki, self.first_term,
        )
        release = reportcards.release_list(ctx)
        row = next(r for r in release["rows"] if r["student"]["name"] == "Chiamaka Nwosu")
        card = reportcards.one_card(ctx, self.tunde.pk)
        tab = student_results.results_tab(self.tenant, self.result_reader, self.amina.pk)
        term = tab["terms"][0]
        return [
            Sample(_SURFACE + "ReleaseRowSerializer", row),
            Sample(_SURFACE + "ReportCardSerializer", card),
            Sample(_SURFACE + "CardSubjectSerializer", card["subjects"][0]),
            Sample(_SURFACE + "ResultSubjectSerializer", term["subjects"][0]),
            Sample(_SURFACE + "ResultTermSerializer", term),
            Sample(_SURFACE + "StudentResultsSerializer", tab),
            Sample(_SURFACE + "ResultHistorySerializer", tab["history"][0]),
        ]
