from django.apps import AppConfig


class VsStudentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_students"
    label = "vs_students"
    verbose_name = "Student Management"

    def ready(self):
        # Its sections of a person's data copy (core.personal_data).
        from .personal_data import register_sections as register_personal_data

        register_personal_data()

        # The Export Centre never imports a domain app; the domain app
        # registers itself. Same shape as vs_schools and vs_academics.
        from . import export_datasets, field_access, media_policies
        from .services import import_registry

        # A pupil or parent record holds its sign-in until it is erased.
        from django.apps import apps as django_apps

        from vs_user.erasure import register_account_holder

        register_account_holder(lambda: django_apps.get_model("vs_students", "Student"))
        register_account_holder(lambda: django_apps.get_model("vs_students", "Guardian"))

        # The student fields an administrator may restrict per role.
        field_access.register()
        # The audit rows whose reason that registry's switch governs.
        field_access.register_audit_words()

        # The rows a student or guardian profile can be read as at an earlier
        # date. The history engine never imports a domain app either.
        from . import history

        history.register()

        # No default policy exists: a file whose owner registers nothing is
        # never served. This is what makes a student photograph readable at
        # all, and what stops it being readable by the wrong branch.
        media_policies.register()

        # The Export Centre and the import engine never import a domain app;
        # the domain app registers itself with them. Same direction as
        # vs_schools and vs_academics.
        export_datasets.register_datasets()
        # After the datasets, because a screen binding names one.
        export_datasets.register_screens()
        import_registry.register()

        # An admission number is accepted wherever the sign-in box takes an
        # email, and a pupil's account links also reach their primary guardian.
        # The account engine is told, and imports nothing from this app.
        from . import sign_in

        sign_in.register()

        # Pupils ask the school to close their sign-in; parents close theirs
        # at once. The account engine is told, and imports nothing from here.
        from . import account_closure

        account_closure.register()
