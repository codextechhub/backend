from django.apps import AppConfig


# Register the payment app metadata with Django.
class VsPaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"  # Use BigAutoField for generated model primary keys.
    name = "vs_payments"  # Point Django at the import path for the payments app.
    verbose_name = "Payments"  # Display a human-readable app name in admin and diagnostics.

    def ready(self):
        # Publish this app's datasets to the Export Centre. Registration lives
        # here, not in vs_exports, so the engine never imports a domain app.
        from .export_datasets import register_datasets

        register_datasets()
        # Screen bindings let a filtered list screen become a one-click export.
        from .export_datasets import register_screens

        register_screens()
        # Declare the payment fields an administrator may restrict per role.
        from .field_access import register as register_field_access

        register_field_access()
        # Publish this tenant's payout-approval ladder when its books are created, so
        # the gate over the highest-risk cash-out path is on from onboarding rather
        # than from a remembered command.
        from vs_finance.provisioning import register_entity_provisioner

        from .provisioning import provision_payout_approval

        register_entity_provisioner(provision_payout_approval)
        # Every new branch gets a payment terminal at the provider, and one that
        # leaves service for good has it switched off.
        from django.db.models.signals import post_save
        from vs_tenants.models import Branch

        from .terminals import on_branch_saved

        post_save.connect(on_branch_saved, sender=Branch,
                          dispatch_uid="vs_payments.terminals.on_branch_saved")
        # Warn the period close about online payments left in gateway clearing.
        from .settlement import register as register_close_checks

        register_close_checks()
        # Refuse a refund recorded as paid online for a tenant whose payments
        # settle directly; finance asks without importing this app.
        from vs_finance.credit_notes import register_refund_guard

        from .custody import refund_guard

        register_refund_guard(refund_guard)
        # Pay a refund tied to an online payment back through the provider; finance
        # hands it over when the refund posts.
        from vs_finance.credit_notes import register_refund_channel

        from .refunds import channel as provider_refund_channel

        register_refund_channel(provider_refund_channel)
        # Money that moved through the gateway is a record kept for the
        # statutory period, like the ledger it posts to.
        from core import retention
        from vs_finance.retention import dated_record

        from .models import (
            CollectionIntent,
            HeldMovement,
            HeldSettlement,
            PayoutBatch,
            PayoutInstruction,
            ProviderRefund,
            TerminalPayment,
        )

        retention.register(CollectionIntent, dated_record("created_at", "an online collection"))
        retention.register(TerminalPayment, dated_record("created_at", "a branch terminal payment"))
        retention.register(PayoutBatch, dated_record("created_at", "a payout batch"))
        retention.register(PayoutInstruction, dated_record("created_at", "a payout"))
        retention.register(ProviderRefund, dated_record("created_at", "a refund through the provider"))
        retention.register(HeldMovement, dated_record("occurred_on", "a held-funds movement"))
        retention.register(HeldSettlement, dated_record("created_at", "a held-funds settlement"))

