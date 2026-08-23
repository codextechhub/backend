from __future__ import annotations

from django.utils import timezone

from ..models import TicketSubscription


def is_following(ticket, user) -> bool:
    subscription = TicketSubscription.objects.filter(
        ticket=ticket,
        user=user,
    ).only("muted_at").first()
    if subscription is not None:
        return subscription.muted_at is None
    return ticket.requester_id == user.pk or ticket.assignee_id == user.pk


def follow(ticket, user, *, source=TicketSubscription.Source.MANUAL):
    subscription, created = TicketSubscription.objects.get_or_create(
        ticket=ticket,
        user=user,
        defaults={"source": source},
    )
    update_fields = []
    if subscription.muted_at is not None:
        subscription.muted_at = None
        update_fields.append("muted_at")
    if source == TicketSubscription.Source.COMMENTED and subscription.source != source:
        subscription.source = source
        update_fields.append("source")
    if update_fields:
        subscription.save(update_fields=[*update_fields, "updated_at"])
    return subscription, created


def unfollow(ticket, user):
    subscription, _ = TicketSubscription.objects.get_or_create(
        ticket=ticket,
        user=user,
        defaults={"source": TicketSubscription.Source.MANUAL},
    )
    if subscription.muted_at is None:
        subscription.muted_at = timezone.now()
        subscription.save(update_fields=["muted_at", "updated_at"])
    return subscription


def active_users(ticket):
    return [
        subscription.user
        for subscription in ticket.subscriptions.filter(muted_at__isnull=True)
        .select_related("user__tenant")
    ]


def muted_user_ids(ticket, user_ids):
    if not user_ids:
        return set()
    return set(
        ticket.subscriptions.filter(
            user_id__in=user_ids,
            muted_at__isnull=False,
        ).values_list("user_id", flat=True)
    )
