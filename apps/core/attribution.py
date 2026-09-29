"""Who did it: the shared wording for an action taken under a proxy.

A proxy (an impersonation session) lets one person act with another person's
authority. The action is valid in the impersonated person's name, and history
must still say whose hands did it: "Ada Obi for Chioma Okafor".

Rows record the pair in one of two shapes, and both reduce to the same three
response fields here:

* audit rows (``FinanceAuditLog``, ``WorkflowAuditLog``) name the real person
  as ``actor`` and the impersonated person as ``effective_user``;
* approval votes (``WorkflowStageAction``) and gateway events
  (``PaymentEvent``) name the impersonated person as the actor, because the
  action ran with that person's authority (eligibility and quorum count their
  vote), and the real person as ``proxied_by``.

Every payload that shows who did something adds the same keys next to the ones
it already had:

``real_actor_name``
    The person who really acted, when the action was proxied; otherwise ``None``.
``proxied_user_name``
    The person whose authority was used, when proxied; otherwise ``None``.
``acted_label``
    Ready to display: "<real> for <impersonated>" when proxied, else the
    actor's own name.
"""
from __future__ import annotations


def person_name(user, default: str = "System") -> str:
    """Display name for a user: full name, then email, then ``default``."""
    if user is None:
        return default
    full = (getattr(user, "full_name", "") or "").strip()
    if not full:
        full = (
            f"{getattr(user, 'first_name', '') or ''} "
            f"{getattr(user, 'last_name', '') or ''}"
        ).strip()
    return full or getattr(user, "email", "") or default


def proxy_attribution(performer, principal=None, *, name=person_name) -> dict:
    """The three attribution fields for one action.

    ``performer`` is the person who physically acted and ``principal`` the
    person being impersonated, or ``None`` when nobody was. ``name`` renders a
    user; callers that avoid emails in a payload pass their own renderer so the
    label matches the names already beside it.
    """
    if principal is None or getattr(principal, "pk", None) == getattr(performer, "pk", None):
        return {
            "real_actor_name": None,
            "proxied_user_name": None,
            "acted_label": name(performer),
        }
    real, proxied = name(performer), name(principal)
    return {
        "real_actor_name": real,
        "proxied_user_name": proxied,
        "acted_label": f"{real} for {proxied}",
    }


def audit_row_attribution(row, *, name=person_name) -> dict:
    """Attribution for an audit row (real ``actor``, impersonated ``effective_user``)."""
    return proxy_attribution(
        row.actor if row.actor_id else None,
        row.effective_user if getattr(row, "effective_user_id", None) else None,
        name=name,
    )


def vote_attribution(vote, *, name=person_name) -> dict:
    """Attribution for an approval vote (impersonated ``actor``, real ``proxied_by``)."""
    if getattr(vote, "proxied_by_id", None):
        return proxy_attribution(vote.proxied_by, vote.actor, name=name)
    return proxy_attribution(vote.actor if vote.actor_id else None, name=name)
