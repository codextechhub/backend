from django.apps import AppConfig


class VsSchoolsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_schools"

    # The package moved under apps/schools/; the label deliberately did not.
    # Django would derive "vs_schools" from the last segment of ``name``
    # anyway, but it is pinned here because every historical migration in
    # nine other apps depends on the label ``vs_schools``, and every table is
    # still named ``vs_schools_*``. Changing it is a database migration, not
    # a rename.
    label = "vs_schools"

    def ready(self):
        from . import signals  # noqa: F401

        # Publish this app's datasets to the Export Centre, and bind its list
        # screen so "Export what this table is showing" can translate its
        # filters. Registration lives here, not in vs_exports, so the engine
        # never imports a domain app.
        from .export_datasets import register_datasets, register_screens

        register_datasets()
        register_screens()
