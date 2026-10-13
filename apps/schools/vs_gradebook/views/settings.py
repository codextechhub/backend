"""The school's gradebook settings.

``GET /v1/gradebook/settings/`` needs ``gradebook.setup.view``. ``PATCH`` needs
``gradebook.setup.update`` and a caller whose reach is the whole school, because
the settings bind every branch: a branch-bound caller holding the key is refused
with a 403 (``SHARED_RECORD_READ_ONLY``) and nothing is written.
"""
from __future__ import annotations

from core.response import success_response

from ..serializers import SettingsWriteSerializer
from ..services import configuration
from .setup import _SetupView


class SettingsView(_SetupView):
    """GET, PATCH /v1/gradebook/settings/

    PATCH takes any of ``show_class_stats``, ``rank_in_class``,
    ``conduct_ratings``, ``rating_key``, ``rating_traits`` and
    ``moderated_programs`` (programme codes) and answers with the GET body. The
    body carries the school's programmes beside the values, each saying whether
    it is moderated.

    docstring-name: Gradebook settings
    """

    def get(self, request):
        return success_response(data=configuration.settings_body(self.tenant))

    def patch(self, request):
        self.assert_may_configure()
        writer = SettingsWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        rules = configuration.write_settings(
            self.tenant, request.user, dict(writer.validated_data),
        )
        return success_response("Gradebook settings saved.", configuration.settings_body(self.tenant, rules))
