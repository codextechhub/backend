"""Checks a domain app runs when somebody's branch reach shrinks.

A role grant decides which branches a person sees. Revoking one, moving it to
another branch, replacing it, or narrowing the role behind it can leave that
person holding work at a branch they no longer see: a class teacher who cannot
find their class. This module does not know what work a person holds and never
imports a domain app to find out, so each app registers a guard here, and every
path that shrinks reach calls :func:`assert_reach_kept` after making the change,
inside the same transaction. A guard refuses by raising, and the change is
undone with it.

Only an administrator's own changes are checked. A system withdrawal (a hire
that was not approved, a super admin transfer) is never refused, because nobody
is there to act on the refusal.
"""
from __future__ import annotations

_GUARDS: list = []


class ReachRefused(Exception):
    """Mixed into a guard's refusal, so a path that must not raise can catch it.

    Approving a role change on the ladder is one: it records the refusal as
    APPLY_FAILED with the guard's message rather than failing the decision.
    """


def register_reach_guard(guard) -> None:
    """Add ``guard(tenant, user)``, which raises when *user* may not lose reach."""
    if guard not in _GUARDS:
        _GUARDS.append(guard)


def assert_reach_kept(tenant, users) -> None:
    """Run every guard for each of *users*, after their grants have changed."""
    for user in users:
        if user is None:
            continue
        for guard in _GUARDS:
            guard(tenant, user)
