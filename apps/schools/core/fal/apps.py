from django.apps import AppConfig


class FalConfig(AppConfig):
    """The FAL is a Django app because it owns one table.

    The label is ``fal`` rather than the package's last segment, and the app is
    ``schools.core.fal`` rather than ``schools.core``: the FAL owns
    :class:`~schools.core.fal.models.FeeStructureTermLink`, and pointing the app
    at the package that owns the model keeps ``models.py`` and ``migrations/``
    where a reader expects to find them. ``schools.core`` stays a plain package.

    ``ready`` connects the FAL to the finance and payments engines' signals: a
    pupil's first posted bill issues their transfer account, a replaced
    number tells the pupil and their primary guardian
    (:mod:`schools.core.fal.transfer_accounts`), and a booked transfer into the
    account tells the pupil and their guardians
    (:mod:`schools.core.fal.deposit_notices`). It also registers the accounts
    a school commonly adds as suggestions for the finance chart
    (:mod:`schools.core.fal.account_suggestions`) and answers how a school asks
    its families to pay (:mod:`schools.core.fal.family_payments`).
    """

    name = "schools.core.fal"
    label = "fal"
    verbose_name = "Finance Abstraction Layer"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from vs_finance.signals import invoice_posted

        from vs_payments.signals import virtual_account_deposit_booked, virtual_account_replaced

        from vs_finance.account_guide import register_account_suggestion_provider
        from vs_finance.document_contacts import register_contact_resolver

        from vs_finance.payer_options import register_payer_options_provider

        from .account_suggestions import school_account_suggestions
        from .deposit_notices import on_virtual_account_deposit_booked
        from .family_payments import payer_options
        from .guardian_invoices import on_invoice_posted as tell_primary_guardian
        from .guardian_invoices import primary_guardian_contact
        from .transfer_accounts import on_invoice_posted, on_virtual_account_replaced

        register_contact_resolver(primary_guardian_contact)
        register_account_suggestion_provider(school_account_suggestions)
        register_payer_options_provider(payer_options)

        invoice_posted.connect(on_invoice_posted, dispatch_uid="fal.transfer_account_on_first_bill")
        invoice_posted.connect(tell_primary_guardian, dispatch_uid="fal.guardian_invoice_on_post")
        virtual_account_replaced.connect(
            on_virtual_account_replaced, dispatch_uid="fal.transfer_account_changed_notice")
        virtual_account_deposit_booked.connect(
            on_virtual_account_deposit_booked, dispatch_uid="fal.transfer_deposit_notice")
