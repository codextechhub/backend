from django.apps import AppConfig


class FalConfig(AppConfig):
    """The FAL is a Django app because it owns one table.

    The label is ``fal`` rather than the package's last segment, and the app is
    ``schools.core.fal`` rather than ``schools.core``: the FAL owns
    :class:`~schools.core.fal.models.FeeStructureTermLink`, and pointing the app
    at the package that owns the model keeps ``models.py`` and ``migrations/``
    where a reader expects to find them. ``schools.core`` stays a plain package.

    ``ready`` connects the FAL to the finance engine's signals: a pupil's first
    posted bill issues their transfer account
    (:mod:`schools.core.fal.transfer_accounts`).
    """

    name = "schools.core.fal"
    label = "fal"
    verbose_name = "Finance Abstraction Layer"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from vs_finance.signals import invoice_posted

        from .transfer_accounts import on_invoice_posted

        invoice_posted.connect(on_invoice_posted, dispatch_uid="fal.transfer_account_on_first_bill")
