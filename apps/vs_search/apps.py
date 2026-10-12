from django.apps import AppConfig


class VsSearchConfig(AppConfig):
    """The product-wide search: screens, settings and people in one answer.

    Holds no models. Places are declared in code by the app that owns each
    screen, in that app's ``search_places.py``, and people are found by the
    module that owns them; see :mod:`vs_search.registry`.
    """

    name = "vs_search"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "Search"
