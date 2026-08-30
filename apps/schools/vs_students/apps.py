from django.apps import AppConfig


class VsStudentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_students"
    label = "vs_students"
    verbose_name = "Student Management"

    def ready(self):
        # The Export Centre never imports a domain app; the domain app
        # registers itself. Same shape as vs_schools and vs_academics.
        from . import export_datasets, media_policies
        from .services import import_registry

        # No default policy exists: a file whose owner registers nothing is
        # never served. This is what makes a student photograph readable at
        # all, and what stops it being readable by the wrong branch.
        media_policies.register()

        # The Export Centre and the import engine never import a domain app;
        # the domain app registers itself with them. Same direction as
        # vs_schools and vs_academics.
        export_datasets.register_datasets()
        import_registry.register()
