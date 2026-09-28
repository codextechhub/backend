from django.apps import AppConfig


class VsConfigConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'vs_config'

    def ready(self):
        # A time zone is this app's own definition, so its guard is registered here.
        from .clock import TIME_ZONE_KEY, guard_time_zone
        from .services.resolution import register_value_guard

        register_value_guard(TIME_ZONE_KEY, guard_time_zone)
