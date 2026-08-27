from django.apps import AppConfig


class VsCalendarConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_calendar"

    # Stated, not inferred: every table and every migration reads it. The app
    # keeps the name the FRD gave it in version 2.1, although it owns the
    # timetable too - the module's identity is its number, not its package.
    label = "vs_calendar"

    def ready(self):
        # Registered here, never in vs_exports: the engine keeps no import of
        # a school-shaped module. Datasets first - a binding names one, so an
        # unregistered dataset would resolve to None at request time rather
        # than at boot.
        from .export_datasets import register_datasets, register_screens

        register_datasets()
        register_screens()
