# receivers.py
# Keeps impersonation state in sync with tenant lifecycle without vs_tenants
# having to import vs_admin_console: when a Tenant leaves ACTIVE, every active
# impersonation session scoped to it must be ended.

from django.db.models.signals import post_save
from django.dispatch import receiver

from vs_tenants.models import Tenant

from .services import end_impersonations_for_tenant


@receiver(post_save, sender=Tenant, dispatch_uid="vs_admin_console.tenant_deactivated")
def on_tenant_saved(sender, instance, **kwargs):
    if instance.status != Tenant.Status.ACTIVE:
        end_impersonations_for_tenant(instance)
