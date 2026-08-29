from django.apps import AppConfig


class FalConfig(AppConfig):
    """The FAL is a Django app because it owns one table.

    The label is ``fal`` rather than the package's last segment, and the app is
    ``schools.core.fal`` rather than ``schools.core``: the FAL owns
    :class:`~schools.core.fal.models.FeeStructureTermLink`, and pointing the app
    at the package that owns the model keeps ``models.py`` and ``migrations/``
    where a reader expects to find them. ``schools.core`` stays a plain package.
    """

    name = "schools.core.fal"
    label = "fal"
    verbose_name = "Finance Abstraction Layer"
    default_auto_field = "django.db.models.BigAutoField"
