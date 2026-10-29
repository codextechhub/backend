"""Which school a scenario seeder fills, and how it keeps that run to itself.

Each scenario seeder builds a cast of development schools by slug. ``--school``
fills another school that already exists instead, such as one onboarded
through the app on a staging server, by following a cast member's recipe,
named with ``--like``. Tables keyed by school (who teaches what, which classes
are billed) are read by the recipe; anything that names a person (an email,
an admission number) is built from the school being filled. The school's own
administrator is named with ``--admin-email``, because a school onboarded for
real has no ``admin@<slug>.example.com``.

Two guards hold for every run:

* A seeder invents people and gives them known passwords, so it refuses to
  run where Paystack takes live keys, which is production
  (``core.dev_guards``).
* ``quiet()`` keeps the run's side effects on the server: background tasks run
  in this process and email goes to Django's in-memory outbox. On a server
  whose worker sends email for real, an invitation to an invented address
  would otherwise leave the building. It also holds back the parent app
  invitations a school sends its guardians automatically, because a seeder
  gives each seeded guardian the access its scenario needs and no more.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from django.conf import settings
from django.core.management.base import CommandError
from django.test.utils import override_settings

from core.dev_guards import refuse_production


@dataclass(frozen=True)
class Target:
    """One school to fill: its slug, the cast member it follows, its administrator."""

    slug: str
    recipe: str
    admin_email: str = ""


def add_target_arguments(parser) -> None:
    """``--only`` for the cast, or ``--school`` with ``--like`` and ``--admin-email``."""
    parser.add_argument("--only", help="One school slug from the cast.")
    parser.add_argument(
        "--school", help="An existing school to fill instead of the cast (staging).",
    )
    parser.add_argument(
        "--like", help="The cast member whose recipe --school follows.",
    )
    parser.add_argument(
        "--admin-email", default="",
        help="The --school administrator's sign-in email.",
    )


def resolve_targets(options, cast) -> list[Target]:
    """The schools this run fills, after the production guard."""
    refuse_production()
    only, school = options.get("only"), options.get("school")
    if school:
        if only:
            raise CommandError("Give --only or --school, not both.")
        like = options.get("like") or ""
        if like not in cast:
            raise CommandError(
                f"--school needs --like, one of: {', '.join(cast)}.",
            )
        return [Target(school, like, (options.get("admin_email") or "").strip())]
    if only and only not in cast:
        raise CommandError(
            f"{only!r} is not in the cast. Pick one of: {', '.join(cast)}",
        )
    return [Target(slug, slug) for slug in ([only] if only else list(cast))]


def admin_email(target: Target) -> str:
    """The administrator's email: the one given, else the development fixture's."""
    return target.admin_email or f"admin@{target.slug}.example.com"


@contextmanager
def quiet():
    """Run background tasks here, keep every email in the in-memory outbox, invite no parent automatically."""
    from apps.celery import app
    from schools.vs_students.services.parent_app_auto import paused

    eager = app.conf.task_always_eager
    propagates = app.conf.task_eager_propagates
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True
    try:
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
            CELERY_TASK_ALWAYS_EAGER=True,
        ), paused():
            yield
    finally:
        app.conf.task_always_eager = eager
        app.conf.task_eager_propagates = propagates
