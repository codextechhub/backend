from __future__ import annotations

from contextvars import ContextVar


_current_tenant = ContextVar("current_tenant", default=None)


def get_current_tenant():
    return _current_tenant.get()


def set_current_tenant(tenant):
    return _current_tenant.set(tenant)


def reset_current_tenant(token):
    _current_tenant.reset(token)


def clear_current_tenant():
    _current_tenant.set(None)
