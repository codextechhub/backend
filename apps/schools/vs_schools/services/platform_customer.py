"""Keep a school registered as a customer in CodeX's own books.

The school owns its operational books, but CodeX is the supplier for the
subscription. The corresponding receivables customer therefore belongs to the
platform entity, not to the school's entity. A loose source reference provides
the durable one-to-one link without making the finance engine import a school
model.

Creation is strict about the customer record: if the platform entity is absent
or the customer cannot be written, the surrounding school-creation transaction
fails. The receivables account may still be absent on a fresh installation, so
the customer is registered without one and invoicing continues to fail closed
until Finance is seeded. Later school saves update an existing customer only.
That keeps legacy ORM fixtures and historical rows usable while every school
created through the product contract remains synchronized.
"""
from rest_framework.exceptions import ValidationError
from django.utils import timezone

from vs_finance.account_mappings import resolve_mapped_account
from vs_finance.constants import AccountMappingKey
from vs_finance.exceptions import MissingAccountError
from vs_finance.models import Customer, LedgerEntity


SOURCE_TYPE = "vs_schools.School"


def _customer_values(school) -> dict:
    return {
        "name": school.name,
        "billing_email": school.email,
        "billing_phone": school.phone,
        "billing_address": school.address,
        "is_active": school.status in {"PENDING", "ACTIVE"},
    }


def register_platform_customer(school) -> Customer:
    """Create or synchronize the platform receivables customer for ``school``."""
    platform = LedgerEntity.objects.platform()
    if platform is None:
        raise ValidationError({
            "school": (
                "CodeX billing is not ready, so the school was not created. "
                "Set up the platform finance entity and try again."
            ),
        })

    try:
        receivable = resolve_mapped_account(
            platform,
            AccountMappingKey.ACCOUNTS_RECEIVABLE,
            label="school subscription receivables",
        )
    except MissingAccountError:
        # The customer master can exist before the platform chart is seeded.
        # Invoicing still fails closed until Finance supplies this account.
        receivable = None

    customer, _created = Customer.objects.update_or_create(
        entity=platform,
        source_type=SOURCE_TYPE,
        source_id=str(school.pk),
        defaults={
            **_customer_values(school),
            "receivable_account": receivable,
        },
    )
    return customer


def sync_registered_platform_customer(school) -> bool:
    """Synchronize an existing linked customer, returning whether one existed."""
    platform = LedgerEntity.objects.platform()
    if platform is None:
        return False
    updated = Customer.objects.filter(
        entity=platform,
        source_type=SOURCE_TYPE,
        source_id=str(school.pk),
    ).update(**_customer_values(school), updated_at=timezone.now())
    return bool(updated)
