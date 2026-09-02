from __future__ import annotations

from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        from . import binding

        binding.connect_all()

        # Importing the module registers its @register() check, which reports a
        # production deployment in which no scheduled task can ever run.
        from . import checks  # noqa: F401
