"""No year-block field reaches a caller who may read none, at any depth.

The two surfaces of the cumulative block, rendered as a caller whose role has
every field of the resource switched off, on the block the report card of
Tunde's third term prints with a position, annual averages, a term total for
every subject and a recorded promotion.
"""
from __future__ import annotations

from schools.vs_gradebook.services import classbook, reportcards
from schools.vs_students.constants import EnrolmentOutcome
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from .cumulative_base import CumulativeFixture

_SURFACE = "schools.vs_gradebook.serializers."


class CumulativeDeepPayloadTests(DeepPayloadChecks, CumulativeFixture):
    resources = frozenset({"gradebook.result", "gradebook.sheet"})
    covers = frozenset({
        _SURFACE + name for name in ("CumulativeSerializer", "CumulativeSubjectSerializer")
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_year()
        cls.record_outcome(cls.tunde, EnrolmentOutcome.PROMOTED, cls.next_jss2a)

    def samples(self):
        ctx = classbook.ClassContext(
            self.tenant, self.releaser, self.jss1a, self.lekki, self.third_term,
        )
        block = reportcards.one_card(ctx, self.tunde.pk)["cumulative"]
        return [
            Sample(_SURFACE + "CumulativeSerializer", block),
            Sample(_SURFACE + "CumulativeSubjectSerializer", block["subjects"][0]),
        ]
