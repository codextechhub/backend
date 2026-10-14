"""No analytics field reaches a caller who may read none, at any depth.

The six analytics surfaces rendered as callers whose role has every field of the
sheet and result resources switched off, on the payload the service builds for
Brightfield's first term (three published classes, a subject compared across
them, three movers and a top for each class).
"""
from __future__ import annotations

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from ..services import analytics
from .analytics_base import AnalyticsFixture

_SURFACE = "schools.vs_gradebook.serializers."


class AnalyticsDeepPayloadTests(DeepPayloadChecks, AnalyticsFixture):
    resources = frozenset({"gradebook.sheet", "gradebook.result"})
    covers = frozenset({
        _SURFACE + name for name in (
            "AnalyticsSerializer", "AnalyticsSubjectSerializer", "AnalyticsArmSerializer",
            "AnalyticsCompareSerializer", "AnalyticsMoverSerializer", "AnalyticsTopSerializer",
        )
    })
    #: The screen's payload nests the sheet's class figures under a result-resource
    #: top level. Their own switch is proved on their own surfaces below.
    allowlist = {
        (_SURFACE + "AnalyticsSerializer", "subjects[].average"):
            "A subject's average, which the sheet's class figures switch governs.",
        (_SURFACE + "AnalyticsSerializer", "compare[].arms[].average"):
            "A compared class's average, which the sheet's class figures switch governs.",
    }

    def samples(self):
        report = analytics.report(self.tenant, self.reader, self.first_term, None)
        return [
            Sample(_SURFACE + "AnalyticsSerializer", report),
            Sample(_SURFACE + "AnalyticsSubjectSerializer", report["subjects"][1]),
            Sample(_SURFACE + "AnalyticsArmSerializer", report["compare"][0]["arms"][0]),
            Sample(_SURFACE + "AnalyticsCompareSerializer", report["compare"][0]),
            Sample(_SURFACE + "AnalyticsMoverSerializer", report["movers"][0]),
            Sample(_SURFACE + "AnalyticsTopSerializer", report["tops"][0]),
        ]
