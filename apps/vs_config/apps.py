from django.apps import AppConfig


class VsConfigConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'vs_config'

    def ready(self):
        # A time zone is this app's own definition, so its guard is registered here.
        from .clock import TIME_ZONE_KEY, guard_time_zone
        from .services.resolution import register_value_guard

        register_value_guard(TIME_ZONE_KEY, guard_time_zone)
        from .field_access import register as register_field_access
        from .audit_access import register_setting_field
        from .platform_settings import ONBOARDING_FIELDS, PROFILE_FIELDS

        register_field_access()
        for name, key in PROFILE_FIELDS.items():
            if name != "name":
                register_setting_field(key, f"platform.profile.{name}")
        for name, key in ONBOARDING_FIELDS.items():
            register_setting_field(key, f"platform.profile.default_{name}")
