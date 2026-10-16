"""No notice field reaches a caller who may read none, at any depth.

Both staff surfaces are rendered on a real published notice, read counts
included, as a caller whose role has every field of ``school.notices`` switched
off, and the whole payload is walked.
"""
from __future__ import annotations

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from ..services import notices as service
from .test_notices import NoticesFixture

_SURFACE = "schools.vs_notices.serializers."


class NoticesDeepPayloadTests(DeepPayloadChecks, NoticesFixture):
    resources = frozenset({"school.notices"})
    covers = frozenset({
        _SURFACE + "NoticeSerializer",
        _SURFACE + "NoticeWriteSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.notice = service.create(
            cls.tenant, actor=cls.admin, title="PTA meeting Saturday",
            body="All parents are welcome.",
            audience=[{"kind": "CLASS", "id": cls.shared_class.pk}], publish_now=True,
        )
        service.mark_read(cls.notice, cls.chiamaka.user)

    def samples(self):
        return [
            Sample(_SURFACE + "NoticeSerializer", self.notice, context={"with_reads": True}),
            Sample(_SURFACE + "NoticeWriteSerializer", {
                "title": "PTA meeting Saturday", "body": "All parents are welcome.",
                "audience": [{"kind": "CLASS", "id": self.shared_class.pk}],
            }),
        ]
