"""The Receivables & collections view of the finance dashboard.

A second tab beside the overview, for the people who chase fees. It reads the
same windows (a school's term, a month, a quarter, the year to date; see
:mod:`vs_finance.dashboard_blocks`) and answers the questions the overview only
summarises: how fast this window's fees are coming in against last time and
against the owner's target, which groups of payers are behind (a school's
classes, through :mod:`vs_finance.payer_groups`), whether reminders are working,
what was given away in concessions and adjustments, what credit payers hold, and
who owes the most.

Every block needs the key behind it and answers under the reader's branches,
as on the overview; a block the reader may not see is ``None``.

The collection curve
    Week by week from the window's start, the share of the window's fees paid
    so far. Money paid before the window opened (a parent paying First Term fees
    in the holiday) counts in the first week. The previous comparable window
    (last term, last month) is drawn over its whole length, so this term's
    position can be read against last term's at the same week, and the owner's
    target (``FinanceDocumentSettings.term_collection_target_pct``) is the line
    both aim at. The projection carries this window forward along last window's
    shape; with no previous window there is no projection.
"""
from __future__ import annotations

import datetime
import statistics
from collections import defaultdict

from django.db.models import Count, DateField, Exists, ExpressionWrapper, F, Max, OuterRef, Q, Sum

from vs_rbac.scoping import UNNARROWED

from .billing_periods import current_billing_period
from .constants import DocumentStatus, InvoicePaymentStatus
from .dashboard_blocks import (
    Window,
    _m,
    _ratio,
    _window_invoices,
    receivables_summary,
    resolve_window,
)

TOP_BALANCES = 6
DUNNING_RESPONSE_DAYS = 7
OLD_CREDIT_DAYS = 90


# --------------------------------------------------------------------------- #
# Windows                                                                     #
# --------------------------------------------------------------------------- #

def _shift_months(day: datetime.date, months: int) -> datetime.date:
    index = day.year * 12 + day.month - 1 + months
    return datetime.date(index // 12, index % 12 + 1, 1)


def previous_window(entity, window: Window) -> Window | None:
    """The window before ``window``, over its whole length, for comparison."""
    if window.invoices is not None:
        period = current_billing_period(entity, window.start - datetime.timedelta(days=1))
        if period is None or period.start >= window.start:
            return None
        return Window(period.key, period.label, period.name, period.start, period.end, period.invoices)
    if window.key == "month":
        start = _shift_months(window.start, -1)
        return Window("month", "Last month", start.strftime("%B %Y"), start, window.start - datetime.timedelta(days=1))
    if window.key == "quarter":
        start = _shift_months(window.start, -3)
        return Window("quarter", "Last quarter", f"Quarter from {start:%B %Y}", start,
                      window.start - datetime.timedelta(days=1))
    if window.key == "year":
        start = _shift_months(window.start, -12)
        return Window("year", "Last year", f"FY from {start:%B %Y}", start, window.start - datetime.timedelta(days=1))
    return None


def _window_end(window: Window) -> datetime.date:
    """The last day a window's curve runs to: a term's end, or a calendar window's."""
    if window.invoices is not None:
        return window.end
    if window.key == "month":
        return _shift_months(window.start, 1) - datetime.timedelta(days=1)
    if window.key == "quarter":
        return _shift_months(window.start, 3) - datetime.timedelta(days=1)
    if window.key == "year":
        return _shift_months(window.start, 12) - datetime.timedelta(days=1)
    return window.end


# --------------------------------------------------------------------------- #
# Collections                                                                 #
# --------------------------------------------------------------------------- #

def _billed(entity, window, scope) -> int:
    agg = _window_invoices(entity, window, scope).aggregate(s=Sum(F("total") - F("amount_credited")))
    return int(agg["s"] or 0)


def _paid_by_day(entity, window, scope) -> dict:
    """``{payment date: kobo}`` settled on the window's invoices."""
    from .models import PaymentAllocation

    invoice_ids = _window_invoices(entity, window, scope).values("id")
    rows = (
        PaymentAllocation.objects.filter(
            invoice_id__in=invoice_ids, payment__status=DocumentStatus.POSTED,
        )
        .values(day=F("payment__payment_date"))
        .annotate(s=Sum("amount"))
    )
    return {r["day"]: int(r["s"] or 0) for r in rows}


def _curve(entity, window, scope, until: datetime.date | None) -> list[float] | None:
    """Cumulative % of the window's fees paid at the end of each week."""
    billed = _billed(entity, window, scope)
    if not billed:
        return None
    by_day = _paid_by_day(entity, window, scope)
    end = _window_end(window)
    weeks = max(1, ((end - window.start).days // 7) + 1)
    points, running = [], 0
    days = sorted(by_day)
    i = 0
    for week in range(weeks):
        week_end = window.start + datetime.timedelta(days=7 * week + 6)
        if until is not None and window.start + datetime.timedelta(days=7 * week) > until:
            break
        while i < len(days) and days[i] <= week_end:
            running += by_day[days[i]]
            i += 1
        points.append(round(running * 100 / billed, 1))
    return points


def collection_curve(entity, window, as_of, scope=UNNARROWED) -> dict | None:
    from .document_settings import resolve_finance_document_settings

    current = _curve(entity, window, scope, until=as_of)
    if current is None:
        return None
    previous = previous_window(entity, window)
    last = _curve(entity, previous, scope, until=None) if previous else None
    end = _window_end(window)
    weeks = max(1, ((end - window.start).days // 7) + 1)
    week_now = len(current)
    projection = None
    if last and len(last) >= week_now:
        projection = round(min(100.0, current[-1] + (last[-1] - last[week_now - 1])), 1)
    target = resolve_finance_document_settings(entity).term_collection_target_pct
    return {
        "weeks": weeks,
        "week_now": week_now,
        "current": current,
        "previous": last,
        "previous_name": previous.name if previous and last else None,
        "target_pct": target,
        "projection_pct": projection,
        "vs_previous_pts": (
            round(current[-1] - last[week_now - 1], 1) if last and len(last) >= week_now else None
        ),
    }


def days_to_pay(entity, window, scope=UNNARROWED) -> int | None:
    """Median days from invoice to its final payment, over the window's settled invoices."""
    from .models import PaymentAllocation

    paid = _window_invoices(entity, window, scope).filter(payment_status=InvoicePaymentStatus.PAID)
    rows = (
        PaymentAllocation.objects.filter(invoice__in=paid, payment__status=DocumentStatus.POSTED)
        .values("invoice_id", "invoice__invoice_date")
        .annotate(last=Max("payment__payment_date"))
    )
    spans = [max((r["last"] - r["invoice__invoice_date"]).days, 0) for r in rows]
    return int(statistics.median(spans)) if spans else None


def credit_held(entity, as_of, scope=UNNARROWED) -> dict:
    """Money payers have with the school that no invoice has used yet."""
    from .models import CreditNote, Payment

    receipts = scope.filter(Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED)).annotate(
        spare=F("amount") - F("allocated_amount") - F("refunded_amount"),
    ).filter(spare__gt=0)
    notes = scope.filter(CreditNote.objects.filter(
        entity=entity, status=DocumentStatus.POSTED, kind="CREDIT",
    )).annotate(spare=F("total") - F("allocated_amount") - F("refunded_amount")).filter(spare__gt=0)
    old = as_of - datetime.timedelta(days=OLD_CREDIT_DAYS)
    r = receipts.aggregate(total=Sum("spare"), n=Count("id"), payers=Count("customer_id", distinct=True),
                           old=Sum("spare", filter=Q(payment_date__lt=old)))
    c = notes.aggregate(total=Sum("spare"), payers=Count("customer_id", distinct=True),
                        old=Sum("spare", filter=Q(note_date__lt=old)))
    return {
        "total": _m(int(r["total"] or 0) + int(c["total"] or 0)),
        "unapplied_receipts": r["n"],
        "unapplied_receipts_amount": _m(r["total"]),
        "credit_notes_amount": _m(c["total"]),
        "payers": (r["payers"] or 0) + (c["payers"] or 0),
        "older_than_days": OLD_CREDIT_DAYS,
        "older_amount": _m(int(r["old"] or 0) + int(c["old"] or 0)),
    }


# --------------------------------------------------------------------------- #
# Plans, groups, reminders, relief                                            #
# --------------------------------------------------------------------------- #

def payment_plans(entity, as_of, scope=UNNARROWED) -> dict:
    from .models import PaymentPlan, PaymentPlanInstallment

    active = scope.filter(PaymentPlan.objects.filter(entity=entity, plan_status="ACTIVE"))
    late = PaymentPlanInstallment.objects.filter(
        plan=OuterRef("pk"), due_date__lt=as_of,
    ).exclude(status="PAID")
    rows = active.annotate(behind=Exists(late)).annotate(
        owed=Sum(F("installments__amount") - F("installments__amount_settled")),
    )
    on_track = behind = 0
    owed_on_track = owed_behind = 0
    for plan in rows:
        if plan.behind:
            behind += 1
            owed_behind += int(plan.owed or 0)
        else:
            on_track += 1
            owed_on_track += int(plan.owed or 0)
    upcoming = (
        PaymentPlanInstallment.objects.filter(plan__in=active, due_date__gte=as_of)
        .exclude(status="PAID")
        .values("due_date")
        .annotate(plans=Count("plan_id", distinct=True), amount=Sum(F("amount") - F("amount_settled")))
        .order_by("due_date")[:3]
    )
    return {
        "active": on_track + behind,
        "on_track": on_track, "on_track_amount": _m(owed_on_track),
        "behind": behind, "behind_amount": _m(owed_behind),
        "next": [{"date": u["due_date"].isoformat(), "plans": u["plans"], "amount": _m(u["amount"])} for u in upcoming],
    }


def by_group(entity, window, scope=UNNARROWED) -> dict | None:
    """Billed and collected per payer group (a school's classes)."""
    from .payer_groups import group_payers

    rows = list(
        _window_invoices(entity, window, scope)
        .values("customer_id")
        .annotate(billed=Sum(F("total") - F("amount_credited")), paid=Sum("amount_paid"))
    )
    grouping = group_payers(entity, [r["customer_id"] for r in rows])
    if grouping is None:
        return None
    totals = defaultdict(lambda: [0, 0])
    for r in rows:
        label = grouping.groups.get(r["customer_id"])
        if label:
            totals[label][0] += int(r["billed"] or 0)
            totals[label][1] += int(r["paid"] or 0)
    if not totals:
        return None
    return {
        "label": grouping.label,
        "items": [
            {"name": name, "billed": _m(b), "collected": _m(p), "rate_pct": _ratio(p, b)}
            for name, (b, p) in sorted(totals.items())
        ],
    }


def dunning(entity, window, as_of, scope=UNNARROWED) -> list[dict]:
    """Reminders sent in the window by stage, and how many were followed by payment."""
    from .models import DunningNotice, PaymentAllocation

    start = window.start
    paid_after = PaymentAllocation.objects.filter(
        invoice_id=OuterRef("invoice_id"), payment__status=DocumentStatus.POSTED,
        payment__payment_date__gte=OuterRef("notice_date"),
        payment__payment_date__lte=OuterRef("respond_by"),
    )
    notices = (
        scope.filter(DunningNotice.objects.filter(
            entity=entity, notice_date__gte=start, notice_date__lte=as_of,
        ).exclude(notice_status="CANCELLED"))
        .annotate(respond_by=ExpressionWrapper(
            F("notice_date") + datetime.timedelta(days=DUNNING_RESPONSE_DAYS), output_field=DateField()))
        .annotate(paid=Exists(paid_after))
    )
    rows = (
        notices.values("level", "stage__name", "stage__min_days_overdue")
        .annotate(sent=Count("id"), paid=Count("id", filter=Q(paid=True)))
        .order_by("level")
    )
    return [
        {
            "level": r["level"], "stage": r["stage__name"] or f"Level {r['level']}",
            "min_days_overdue": r["stage__min_days_overdue"],
            "sent": r["sent"], "paid_within_days": DUNNING_RESPONSE_DAYS,
            "paid_pct": _ratio(r["paid"], r["sent"]),
        }
        for r in rows
    ]


def concessions(entity, window, as_of, scope=UNNARROWED) -> dict:
    from .models import Concession

    qs = scope.filter(Concession.objects.filter(entity=entity, status=DocumentStatus.POSTED))
    if window.invoices is not None:
        qs = qs.filter(invoice__in=_window_invoices(entity, window, UNNARROWED))
    else:
        qs = qs.filter(concession_date__gte=window.start, concession_date__lte=as_of)
    kinds = {k: label for k, label in Concession._meta.get_field("kind").choices}
    rows = qs.values("kind").annotate(amount=Sum("amount"), payers=Count("customer_id", distinct=True))
    total = sum(int(r["amount"] or 0) for r in rows)
    billed = _billed(entity, window, scope)
    return {
        "total": _m(total),
        "payers": qs.aggregate(n=Count("customer_id", distinct=True))["n"],
        "share_of_billed_pct": _ratio(total, billed),
        "items": sorted(
            ({"kind": r["kind"], "label": kinds.get(r["kind"], r["kind"]).split(" (")[0],
              "amount": _m(r["amount"]), "payers": r["payers"]} for r in rows),
            key=lambda i: -i["amount"]["kobo"],
        ),
    }


def adjustments(entity, window, as_of, reader) -> list[dict]:
    """Credit and debit notes, refunds and write-offs in the window, each for its key holders."""
    from .models import CreditNote, Refund, WriteOffRequest

    scope = reader.scope
    start, end = window.start, as_of
    out = []
    if reader.can("finance.creditnote.view"):
        notes = scope.filter(CreditNote.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, note_date__gte=start, note_date__lte=end,
        ))
        for kind, label in (("CREDIT", "Credit notes"), ("DEBIT", "Debit notes")):
            agg = notes.filter(kind=kind).aggregate(n=Count("id"), amount=Sum("total"))
            if agg["n"]:
                out.append({"key": kind.lower() + "_notes", "label": label, "count": agg["n"],
                            "pending": 0, "amount": _m(agg["amount"])})
    if reader.can("finance.refund.view"):
        refunds = scope.filter(Refund.objects.filter(entity=entity, refund_date__gte=start, refund_date__lte=end))
        done = refunds.filter(status=DocumentStatus.POSTED).aggregate(n=Count("id"), amount=Sum("amount"))
        pending = refunds.filter(status__in=[DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL]).count()
        if done["n"] or pending:
            out.append({"key": "refunds", "label": "Refunds paid", "count": done["n"],
                        "pending": pending, "amount": _m(done["amount"])})
    if reader.can("finance.writeoff.view"):
        writeoffs = scope.filter(WriteOffRequest.objects.filter(entity=entity, created_at__date__gte=start))
        done = writeoffs.filter(status=DocumentStatus.POSTED).aggregate(n=Count("id"), amount=Sum("amount"))
        pending = writeoffs.filter(status__in=[DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL]).count()
        if done["n"] or pending:
            out.append({"key": "write_offs", "label": "Write-offs", "count": done["n"],
                        "pending": pending, "amount": _m(done["amount"])})
    return out


def largest_balances(entity, as_of, scope=UNNARROWED) -> list[dict]:
    """The payers owing the most, their balance by age, and the last thing done about it."""
    from .models import DunningNotice, Invoice, Payment, PaymentPlan
    from .payer_groups import group_payers

    open_invoices = (
        scope.filter(Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED))
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .annotate(bal=F("total") - F("amount_paid") - F("amount_credited"))
        .filter(bal__gt=0)
    )
    d30 = as_of - datetime.timedelta(days=30)
    d90 = as_of - datetime.timedelta(days=90)
    top = list(
        open_invoices.values("customer_id", "customer__name", "customer__code", "customer__branch__name")
        .annotate(
            owed=Sum("bal"),
            current=Sum("bal", filter=Q(due_date__gte=as_of)),
            d1_30=Sum("bal", filter=Q(due_date__lt=as_of, due_date__gte=d30)),
            d31_90=Sum("bal", filter=Q(due_date__lt=d30, due_date__gte=d90)),
            over_90=Sum("bal", filter=Q(due_date__lt=d90)),
        )
        .order_by("-owed")[:TOP_BALANCES]
    )
    ids = [r["customer_id"] for r in top]
    grouping = group_payers(entity, ids)
    notices = {}
    for n in DunningNotice.objects.filter(customer_id__in=ids).exclude(notice_status="CANCELLED") \
            .select_related("stage").order_by("customer_id", "-notice_date"):
        notices.setdefault(n.customer_id, n)
    plans = {}
    for p in PaymentPlan.objects.filter(customer_id__in=ids, plan_status="ACTIVE").order_by("customer_id", "-start_date"):
        plans.setdefault(p.customer_id, p)
    receipts = dict(
        Payment.objects.filter(customer_id__in=ids, status=DocumentStatus.POSTED)
        .values("customer_id").annotate(last=Max("payment_date")).values_list("customer_id", "last")
    )

    def last_action(cid):
        options = []
        if cid in notices:
            n = notices[cid]
            options.append((n.notice_date, f"{n.stage.name if n.stage else 'Reminder'} {n.notice_date:%-d %b}"))
        if cid in plans:
            options.append((plans[cid].start_date, f"On a plan from {plans[cid].start_date:%-d %b}"))
        if cid in receipts and receipts[cid]:
            options.append((receipts[cid], f"Paid {receipts[cid]:%-d %b}"))
        return max(options)[1] if options else "No action yet"

    return [
        {
            "customer_id": r["customer_id"], "name": r["customer__name"], "code": r["customer__code"],
            "branch": r["customer__branch__name"],
            "group": grouping.groups.get(r["customer_id"]) if grouping else None,
            "owed": _m(r["owed"]), "current": _m(r["current"]), "days_1_30": _m(r["d1_30"]),
            "days_31_90": _m(r["d31_90"]), "over_90": _m(r["over_90"]),
            "last_action": last_action(r["customer_id"]),
        }
        for r in top
    ]


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def receivables_view(entity, *, reader, window=None, period=None, user=None) -> dict:
    """The Receivables & collections payload for ``entity`` as ``reader`` may see it."""
    from . import dashboard as base
    from .dashboard_blocks import books_kind, collections

    current = base._current_period(entity, period)
    as_of = current.end_date if period is not None and current is not None else datetime.date.today()
    chosen, windows = resolve_window(entity, as_of, current, window)
    scope = reader.scope
    invoices = reader.can("finance.invoice.view")
    payments = reader.can("finance.payment.view")
    return {
        "entity": entity.code,
        "books": books_kind(entity),
        "reader_first_name": (getattr(user, "first_name", "") or "").strip() or None,
        "as_of": as_of.isoformat(),
        "narrowed": not reader.whole_tenant,
        "window": chosen.payload(),
        "windows": [{"key": w.key, "label": w.label, "name": w.name} for w in windows],
        "collections": (
            collections(entity, chosen, scope=scope, billed_ok=invoices, collected_ok=payments)
            if invoices or payments else None
        ),
        "days_to_pay": days_to_pay(entity, chosen, scope) if invoices else None,
        "receivables_summary": receivables_summary(entity, as_of, scope=scope) if invoices else None,
        "credit": credit_held(entity, as_of, scope) if payments else None,
        "curve": collection_curve(entity, chosen, as_of, scope) if invoices and payments else None,
        "plans": payment_plans(entity, as_of, scope) if reader.can("finance.paymentplan.view") else None,
        "groups": by_group(entity, chosen, scope) if invoices else None,
        "dunning": dunning(entity, chosen, as_of, scope) if reader.can("finance.dunning.view") else None,
        "concessions": concessions(entity, chosen, as_of, scope) if reader.can("finance.concession.view") else None,
        "adjustments": adjustments(entity, chosen, as_of, reader),
        "largest": largest_balances(entity, as_of, scope) if invoices else None,
    }
