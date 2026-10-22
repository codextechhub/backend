"""Content negotiation that never refuses a request over its Accept header.

Every API view renders JSON, and a view that serves a file (a payslip PDF, an
attachment, a CSV export) returns the file's bytes as a plain ``HttpResponse``
that no renderer touches. DRF's stock negotiation runs before the view, so a
client that asks for the file's own type (``Accept: application/pdf``) is
refused with 406 "Could not satisfy the request Accept header." before the
view can serve the very file it asked for.

``FallbackContentNegotiation`` keeps DRF's choice whenever the Accept header
names a renderer the view has, and otherwise answers with the view's first
renderer (JSON). A file view still returns its file, and an error from it (a
refusal, a missing payslip) arrives as the usual JSON envelope the frontends
read. A ``?format=`` naming no renderer still raises 404, as DRF does.
"""

from rest_framework.exceptions import NotAcceptable
from rest_framework.negotiation import DefaultContentNegotiation


class FallbackContentNegotiation(DefaultContentNegotiation):
    """DRF's negotiation, falling back to the first renderer instead of 406."""

    def select_renderer(self, request, renderers, format_suffix=None):
        try:
            return super().select_renderer(request, renderers, format_suffix)
        except NotAcceptable:
            renderer = renderers[0]
            return renderer, renderer.media_type
