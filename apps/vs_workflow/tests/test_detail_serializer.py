"""The instance detail serializer builds each object's summary from that object."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from vs_workflow.serializers import WorkflowInstanceDetailSerializer


class DocumentSummaryCacheTests(SimpleTestCase):
    def test_unsaved_instances_do_not_share_a_summary(self):
        """Two unsaved instances share ``pk=None`` but are different objects.

        One serializer reads both in turn, as a caller reusing it would, and
        each must come back with its own snapshot rather than the first one's.
        """
        serializer = WorkflowInstanceDetailSerializer()
        first = SimpleNamespace(
            pk=None,
            document_summary={"title": "First request"},
            document_type="unregistered.type",
            document=object(),
        )
        second = SimpleNamespace(
            pk=None,
            document_summary={"title": "Second request"},
            document_type="unregistered.type",
            document=object(),
        )

        self.assertEqual(serializer.get_document_summary(first)["title"], "First request")
        self.assertEqual(serializer.get_document_summary(second)["title"], "Second request")
        self.assertIsNone(serializer.get_source_document_link(second))
