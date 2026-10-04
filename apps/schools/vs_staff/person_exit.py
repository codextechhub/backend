"""School employment status provider for represented-user payloads."""

from core.person_exit import register_exit_lookup
from vs_tenants.models import Tenant

from .constants import EmploymentStatus
from .models import StaffProfile


def _exited(tenant, user_ids: set[int]) -> set[int]:
    return set(StaffProfile.objects.filter(
        tenant=tenant, user_id__in=user_ids,
        employment_status__in=(EmploymentStatus.RESIGNED, EmploymentStatus.TERMINATED),
    ).values_list("user_id", flat=True))


def _bulk_exited(tenant_groups) -> set[int]:
    user_ids = {user_id for _, group in tenant_groups for user_id in group}
    tenant_ids = {tenant.pk for tenant, _ in tenant_groups}
    return set(StaffProfile.objects.filter(
        tenant_id__in=tenant_ids, user_id__in=user_ids,
        employment_status__in=(EmploymentStatus.RESIGNED, EmploymentStatus.TERMINATED),
    ).values_list("user_id", flat=True))


def register() -> None:
    register_exit_lookup(Tenant.Kind.SCHOOL, _exited, bulk_lookup=_bulk_exited)
