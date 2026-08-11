from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules

class VsWorkflowConfig(AppConfig):
    name = "vs_workflow"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "Workflow Approval Engine"

    def ready(self):
        from vs_workflow import signals  # noqa: F401
        autodiscover_modules("workflow_handlers")
        autodiscover_modules("workflow_conditions")

        # Publish this app's datasets to the Export Centre. Registration lives
        # here, not in vs_exports, so the engine never imports a domain app.
        from .export_datasets import register_datasets

        register_datasets()
