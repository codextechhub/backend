from __future__ import annotations

from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        from . import binding

        binding.connect_all()
