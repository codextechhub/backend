from django.apps import AppConfig


class VsHistoryConfig(AppConfig):
    """The record history engine.

    Holds no declarations of its own: each domain app declares the models it
    keeps a history of from its own ``ready()``, so this app never imports one.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_history"
    verbose_name = "Record History"
