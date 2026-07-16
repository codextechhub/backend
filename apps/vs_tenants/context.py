from __future__ import annotations

from contextvars import ContextVar


_current_tenant = ContextVar("current_tenant", default=None)
_current_audit_actor = ContextVar("current_audit_actor", default=None)
_current_effective_user = ContextVar("current_effective_user", default=None)
_current_impersonation_session = ContextVar("current_impersonation_session", default=None)
_current_audit_event_count = ContextVar("current_audit_event_count", default=0)


def get_current_tenant():
    return _current_tenant.get()


def set_current_tenant(tenant):
    return _current_tenant.set(tenant)


def reset_current_tenant(token):
    _current_tenant.reset(token)


def clear_current_tenant():
    _current_tenant.set(None)
    # This function predates dual-identity context and is already the cleanup
    # hook used by authentication tests and request boundaries. Clearing both
    # prevents a proxy identity surviving after its tenant scope is removed.
    _current_audit_actor.set(None)
    _current_effective_user.set(None)
    _current_impersonation_session.set(None)
    _current_audit_event_count.set(0)


def set_current_audit_identity(*, actor_user, effective_user, impersonation_session=None):
    """Store the dual identity resolved for the current authenticated request."""
    _current_audit_actor.set(actor_user)
    _current_effective_user.set(effective_user)
    _current_impersonation_session.set(impersonation_session)


def get_current_audit_identity():
    """Return ``(actor, effective user, proxy session)`` for this context."""
    return (
        _current_audit_actor.get(),
        _current_effective_user.get(),
        _current_impersonation_session.get(),
    )


def mark_audit_event_emitted():
    """Record that this request already produced a meaningful audit event."""
    _current_audit_event_count.set(_current_audit_event_count.get() + 1)


def get_current_audit_event_count() -> int:
    return _current_audit_event_count.get()


def _same_user(left, right) -> bool:
    left_pk = getattr(left, "pk", None)
    right_pk = getattr(right, "pk", None)
    return left_pk is not None and left_pk == right_pk


def resolve_audit_identity(actor_user, effective_user=None, impersonation_session=None):
    """Resolve durable audit attribution for the current proxy request.

    Events attributed to either request identity are recorded against the
    real actor. System and explicitly third-party-attributed events remain
    untouched.
    """
    request_actor, request_effective, request_session = get_current_audit_identity()
    if request_session is None:
        return actor_user, effective_user, impersonation_session
    if actor_user is None or not (
        _same_user(actor_user, request_actor)
        or _same_user(actor_user, request_effective)
    ):
        return actor_user, effective_user, impersonation_session
    return request_actor, request_effective, request_session


def add_proxy_audit_metadata(metadata, effective_user, impersonation_session):
    """Copy metadata and append portable proxy attribution when applicable."""
    resolved = dict(metadata or {})
    if impersonation_session is not None:
        resolved.update({
            "impersonation_session_id": impersonation_session.pk,
            "effective_user_id": getattr(effective_user, "pk", None),
        })
    return resolved


def clear_current_audit_identity():
    _current_audit_actor.set(None)
    _current_effective_user.set(None)
    _current_impersonation_session.set(None)
    _current_audit_event_count.set(0)


def clear_request_context():
    """Clear every request-local tenant and dual-identity value."""
    clear_current_tenant()
