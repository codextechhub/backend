"""Finance sections of a personal data copy (:mod:`core.personal_data`).

A pupil's fees account: its invoices and payments. A staff member's pay: the
salary record, each payroll line and the bank account pay goes to.
"""
from __future__ import annotations

from core.personal_data import PUPIL, STAFF, register, row, rows

INVOICE_FIELDS = ("document_number", "invoice_date", "due_date", "status", "payment_status",
                  "total", "amount_paid")
PAYMENT_FIELDS = ("document_number", "payment_date", "method", "amount", "status")


def _pick(obj, names):
    from core.personal_data import value

    from django.utils.text import capfirst

    return {capfirst(str(obj._meta.get_field(n).verbose_name)): value(obj._meta.get_field(n), obj)
            for n in names}


def _fees(pupil):
    from schools.core.fal.contracts import SOURCE_TYPE_STUDENT

    from .models import Customer, Invoice, Payment

    out = []
    for customer in Customer._base_manager.filter(
            source_type=SOURCE_TYPE_STUDENT, source_id=str(pupil.pk)):
        out.append({
            "Account": str(customer),
            "Invoices": [_pick(i, INVOICE_FIELDS) for i in
                                           Invoice._base_manager.filter(customer=customer).order_by("pk")],
            "Payments": [_pick(p, PAYMENT_FIELDS) for p in
                                           Payment._base_manager.filter(customer=customer).order_by("pk")],
        })
    return out


def _pay(staff):
    from .models import EmployeeSalary, PayrollLine, StaffBankAccount

    user = staff.user
    if user is None:
        return {}
    bank = StaffBankAccount._base_manager.filter(employee=user).first()
    return {
        "Salary records": rows(EmployeeSalary._base_manager.filter(employee=user).order_by("pk"),
                               skip=("employee",)),
        "Payroll lines": rows(
            PayrollLine._base_manager.filter(employee=user).order_by("pk"), skip=("employee",)),
        "Pay bank account": row(bank, skip=("employee",)) if bank else None,
    }


def register_sections() -> None:
    register(PUPIL, "fees", "Fees account", _fees)
    register(STAFF, "pay", "Pay", _pay)
