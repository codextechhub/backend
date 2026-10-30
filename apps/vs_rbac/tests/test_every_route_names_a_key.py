"""Every route guarded by HasRBACPermission names a key, or says why it needs none.

HasRBACPermission refuses a view that names neither, so a route that forgot
its key fails closed in production. This finds it first: for every route and
every method it answers, it runs the view's own ``get_permissions()`` (which is
where most views choose their key, often per method) and fails on any that
reaches HasRBACPermission with no ``rbac_permission``, no
``rbac_group_permission`` and no ``rbac_open_reason``.
"""
from __future__ import annotations

from django.test import RequestFactory, SimpleTestCase
from django.urls import get_resolver

from vs_rbac.permissions import HasRBACPermission

METHODS = ("get", "post", "put", "patch", "delete")


def _guards(permission) -> bool:
    """Whether an instantiated (possibly composed) permission includes HasRBACPermission."""
    if isinstance(permission, HasRBACPermission):
        return True
    return any(
        _guards(getattr(permission, part))
        for part in ("op1", "op2")
        if getattr(permission, part, None) is not None
    )


def _routes(patterns, prefix=""):
    for pattern in patterns:
        if hasattr(pattern, "url_patterns"):
            yield from _routes(pattern.url_patterns, prefix + str(pattern.pattern))
        else:
            yield prefix + str(pattern.pattern), pattern


class EveryRouteNamesAKeyTests(SimpleTestCase):
    def test_no_route_reaches_the_rbac_check_with_nothing_to_check(self):
        factory = RequestFactory()
        keyless = []
        for route, pattern in _routes(get_resolver().url_patterns):
            callback = pattern.callback
            view_class = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
            if view_class is None:
                continue
            actions = getattr(callback, "actions", None)
            initkwargs = getattr(callback, "initkwargs", None) or getattr(callback, "view_initkwargs", {}) or {}
            methods = list(actions) if actions else [m for m in METHODS if hasattr(view_class, m)]
            for method in methods:
                view = view_class(**initkwargs)
                if actions:
                    view.action_map = actions
                    view.action = actions[method]
                view.args = ()
                view.kwargs = {name: 1 for name in pattern.pattern.regex.groupindex}
                view.format_kwarg = None
                view.request = view.initialize_request(
                    factory.generic(method.upper(), "/", "{}", content_type="application/json"),
                )
                permissions = view.get_permissions()
                if not any(_guards(permission) for permission in permissions):
                    continue
                if not (
                    getattr(view, "rbac_permission", None)
                    or getattr(view, "rbac_group_permission", None)
                    or getattr(view, "rbac_open_reason", None)
                ):
                    keyless.append(f"{method.upper()} {route} ({view_class.__name__})")

        self.assertEqual(keyless, [], "Name a key, or an rbac_open_reason, on:\n" + "\n".join(keyless))
