"""The school's gradebook settings.

``GET /v1/gradebook/settings/`` needs ``gradebook.setup.view``. ``PATCH`` needs
``gradebook.setup.update`` and a caller whose reach is the whole school, because
the settings bind every branch: a branch-bound caller holding the key is refused
with a 403 (``SHARED_RECORD_READ_ONLY``) and nothing is written.
"""
from __future__ import annotations

from core.response import success_response

from ..serializers import SettingsSerializer, SettingsWriteSerializer
from ..services import configuration
from .setup import _SetupView


class SettingsView(_SetupView):
    """GET, PATCH /v1/gradebook/settings/

    PATCH takes any of ``show_class_stats``, ``rank_in_class``,
    ``conduct_ratings``, ``rating_key``, ``rating_traits``,
    ``moderated_programs`` (programme codes) and ``tell_every_parent`` and
    answers with the GET body. The body carries the school's programmes beside
    the values, each saying whether it is moderated. ``tell_every_parent``
    follows its Field Access switch (``gradebook.setup.tell_every_parent``):
    absent for a role that may not read it, and refused in a body from a role
    that may not change it.

    docstring-name: Gradebook settings
    """

    def render(self, body) -> dict:
        return SettingsSerializer(body, context={"request": self.request}).data

    def get(self, request):
        return success_response(data=self.render(configuration.settings_body(self.tenant)))

    def patch(self, request):
        self.assert_may_configure()
        writer = SettingsWriteSerializer(data=request.data, context={"request": request})
        writer.is_valid(raise_exception=True)
        rules = configuration.write_settings(
            self.tenant, request.user, dict(writer.validated_data),
        )
        return success_response(
            "Gradebook settings saved.", self.render(configuration.settings_body(self.tenant, rules)),
        )
