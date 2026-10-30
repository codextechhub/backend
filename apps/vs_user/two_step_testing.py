"""Switching two-step sign-in off for a test class about something else.

Sessions, lockouts, cookies and plan limits are tested by signing CodeX staff
and school administrators in, and every one of them is now asked for a code
after the password. Those tests are about what happens after a sign-in, so
they say so with ``@without_two_step`` rather than each typing a code.
Two-step sign-in itself is tested in ``test_two_step``, with it on.
"""
from __future__ import annotations

from unittest import mock

_TARGET = "vs_user.services.two_step.required_for"


def without_two_step(cls):
    """Nobody is asked for a code while ``cls`` runs, its class fixtures included."""
    set_up = cls.setUpClass.__func__
    tear_down = cls.tearDownClass.__func__

    def setUpClass(klass):
        patcher = mock.patch(_TARGET, return_value=False)
        patcher.start()
        klass.__dict__.get("_two_step_patchers") or setattr(klass, "_two_step_patchers", [])
        klass._two_step_patchers.append(patcher)
        try:
            set_up(klass)
        except BaseException:
            klass._two_step_patchers.pop().stop()
            raise

    def tearDownClass(klass):
        try:
            tear_down(klass)
        finally:
            klass._two_step_patchers.pop().stop()

    cls.setUpClass = classmethod(setUpClass)
    cls.tearDownClass = classmethod(tearDownClass)
    return cls
