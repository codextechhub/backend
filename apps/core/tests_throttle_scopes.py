"""Every throttle scope a view names must have a rate somewhere in settings.

DRF resolves a scope's rate at request time and raises ImproperlyConfigured when
the scope has no entry at all, so a scope without a rate is not an endpoint with
no limit - it is an endpoint that answers 500 to everybody.

The failure is invisible in the place it is introduced. A settings module that
replaces ``DEFAULT_THROTTLE_RATES`` wholesale, or a rate deleted from ``base.py``
along with the view that used to want it, leaves the code untouched and every
test that never hits that route passing. What it costs is on the other side:

    Mrs Nwosu opens the pay link in her fee reminder to settle Chidi's term. The
    scope behind the public pay page has no rate on that deployment, so instead
    of the amount outstanding she gets a server error, and so does every other
    parent Corona Secondary School invoiced that morning.

So this walks the live URL configuration rather than a list somebody maintains,
because the whole point is to catch the scope nobody remembered.
"""
from django.conf import settings
from django.test import SimpleTestCase
from django.urls import get_resolver


def _view_classes():
    """Every view class reachable from the root URL configuration.

    Views are collected by class rather than by route: one class answering six
    URLs names the same scope six times, and a duplicate proves nothing.
    """
    seen = []

    def walk(patterns):
        for pattern in patterns:
            children = getattr(pattern, "url_patterns", None)
            if children is not None:
                walk(children)
                continue
            # DRF's as_view() records the class it came from; a plain Django
            # function view has no scope to declare and nothing to check.
            view = getattr(pattern.callback, "cls", None)
            if view is not None and view not in seen:
                seen.append(view)

    walk(get_resolver().url_patterns)
    return seen


def _scopes(view):
    """The scopes one view asks for, from both places a view can ask.

    ``throttle_scope`` is read by ScopedRateThrottle; a throttle class of its own
    carries its scope as a class attribute. A view can do both, and the pay-link
    routes do: one limit bounds the caller's address, the other bounds the link.
    """
    found = {getattr(view, "throttle_scope", None)}
    for throttle in getattr(view, "throttle_classes", None) or ():
        found.add(getattr(throttle, "scope", None))
    return {scope for scope in found if scope}


def _builds_own_throttles(view):
    """Whether *view*, or a project class it inherits from, chooses its throttles.

    DRF's own ``APIView`` sets ``throttle_classes`` from the default list, so
    classes from ``rest_framework`` do not count; a project base class that
    names them does, as does overriding ``get_throttles``.
    """
    for klass in view.__mro__:
        if klass.__module__.startswith("rest_framework"):
            continue
        if "throttle_classes" in vars(klass) or "get_throttles" in vars(klass):
            return True
    return False


class ThrottleScopeTests(SimpleTestCase):
    def test_every_scope_a_view_names_has_a_rate_in_the_running_settings(self):
        rates = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES") or {}

        missing = sorted({
            f"{scope} ({view.__module__}.{view.__name__})"
            for view in _view_classes()
            for scope in _scopes(view)
            if scope not in rates
        })

        self.assertEqual(
            missing, [],
            "These views name a throttle scope that no rate is declared for, so "
            "every request to them raises rather than being served: "
            + repr(missing),
        )

    def test_every_view_that_names_a_scope_builds_its_own_throttle(self):
        """A scope is only counted by a throttle the view itself builds.

        ``throttle_scope`` alone is read by whatever ``DEFAULT_THROTTLE_CLASSES``
        holds, and a settings module may empty that list. The view then keeps
        its scope and loses its limit, silently:

            The CI and test settings empty the default list, so the public
            account-deletion page answered every request with no limit there,
            while staging limited it. Its throttle tests failed in CI and
            passed on a developer's machine, and the route's protection hung on
            one line in a settings file.

        So a view naming a scope must also name its throttle classes, or build
        them in ``get_throttles``, in its own code rather than inherit DRF's.
        """
        missing = sorted(
            f"{view.__module__}.{view.__name__}"
            for view in _view_classes()
            if getattr(view, "throttle_scope", None) and not _builds_own_throttles(view)
        )

        self.assertEqual(
            missing, [],
            "These views name a throttle scope but leave the throttle to "
            "DEFAULT_THROTTLE_CLASSES, so a settings module that empties it "
            "removes their limit: " + repr(missing),
        )

    def test_security_sensitive_public_routes_are_among_the_scopes_this_covers(self):
        """A walk that silently finds nothing would pass this file forever.

        Public pay and ID-card login are the routes this guard protects, so
        their scopes are named here. If the walk stops reaching views, this
        fails rather than the whole file quietly asserting nothing.
        """
        found = {scope for view in _view_classes() for scope in _scopes(view)}

        self.assertLessEqual(
            {"invoice_pay", "invoice_pay_start",
             "invoice_pay_link", "invoice_pay_link_read",
             "login_preview", "card_login_preview", "card_login"},
            found,
        )
