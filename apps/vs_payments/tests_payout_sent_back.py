"""A payout batch says where it stands with its approval route, as finance documents do.

An approver who returns a batch hands it back as a draft, so its own status
cannot say it is waiting on its sender; ``approval_state`` and
``approval_returned`` say so on the list and the detail, and the detail names
the request the sender resumes (``workflow_instance_id``).
"""
from __future__ import annotations

from django.test import SimpleTestCase

from .serializers import PayoutBatchSerializer, PayoutBatchSummarySerializer


class PayoutBatchReadShapeTests(SimpleTestCase):

    def test_list_and_detail_carry_the_approval_fields(self):
        for serializer in (PayoutBatchSerializer, PayoutBatchSummarySerializer):
            with self.subTest(serializer=serializer.__name__):
                self.assertIn("approval_state", serializer.Meta.fields)
                self.assertIn("approval_returned", serializer.Meta.fields)

