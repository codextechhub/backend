"""Calculate an immutable subscription charge preview for one billing date."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import Student

from ..models import School


@dataclass(frozen=True)
class SubscriptionChargeSnapshot:
    """The quantity and agreed rate that an invoice would preserve."""

    billing_date: date
    active_students: int
    billable_students: int
    price_per_student: int
    amount: int

    @property
    def should_invoice(self) -> bool:
        return self.amount > 0


def calculate_subscription_charge(
    school: School, *, billing_date: date,
) -> SubscriptionChargeSnapshot:
    """Price a school without creating an invoice or changing its books.

    Only students whose current lifecycle status is ACTIVE count. Applicants,
    enrolled but unplaced students, suspended students and former students do
    not increase the bill. The contracted minimum can raise the quantity above
    the active count. With the default minimum of zero, a new empty school has
    no charge and therefore no first invoice.
    """
    setup = school.package_setup
    if not setup.is_active:
        raise ValueError("The school's subscription is inactive.")
    if billing_date < setup.subscription_starts_at:
        raise ValueError("The billing date is before the subscription start date.")
    if billing_date > setup.subscription_expires_at:
        raise ValueError("The billing date is after the subscription expiry date.")
    if setup.agreed_price_per_student is None:
        raise ValueError("The school has no agreed per-student price.")

    active_students = Student.all_objects.filter(
        tenant=school.tenant,
        status=StudentStatus.ACTIVE,
    ).count()
    billable_students = max(active_students, setup.minimum_billable_students)
    return SubscriptionChargeSnapshot(
        billing_date=billing_date,
        active_students=active_students,
        billable_students=billable_students,
        price_per_student=setup.agreed_price_per_student,
        amount=billable_students * setup.agreed_price_per_student,
    )
