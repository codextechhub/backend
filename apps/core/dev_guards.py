"""Where a command that writes known passwords, or destroys data, may run.

Every seeder gives the people it invents a password written in this
repository, so anyone who reads the code can sign in as them. Two lines
follow from that:

* **Production never.** No seeder runs where Paystack takes live keys. A
  deployed server must state ``PAYSTACK_KEY_MODE`` and refuses to start when
  its keys are the other kind (``apps.settings.paystack_keys``), so "live"
  here is production and nothing else.
* **No deployed server, for platform staff.** A seeder that creates or resets
  CodeX staff accounts runs only on a developer's machine or in CI. Staging is
  reachable from the internet, and a console account there with a known
  password reaches every school on it. The school scenario seeders may still
  fill a staging school (see ``schools.vs_schools.dev.targets``), because
  their people belong to that one school.

A deployed server is one running ``apps.settings.staging``, which production
and staging both run.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import CommandError

DEPLOYED_SETTINGS_MODULE = "apps.settings.staging"


def on_deployed_server() -> bool:
    """True on staging and production, false on a developer's machine, in tests and in CI."""
    return getattr(settings, "SETTINGS_MODULE", "") == DEPLOYED_SETTINGS_MODULE


def on_production() -> bool:
    """True where Paystack takes live keys."""
    return str(getattr(settings, "PAYSTACK_KEY_MODE", "") or "").strip().lower() == "live"


def refuse_production() -> None:
    """Stop the run on a server that takes live Paystack keys."""
    if on_production():
        raise CommandError(
            "This server takes live Paystack keys, so it is production. This "
            "command writes known passwords or destroys data, and never runs here.",
        )


def refuse_deployed_server() -> None:
    """Stop the run on staging and production alike."""
    refuse_production()
    if on_deployed_server():
        raise CommandError(
            "This is a deployed server. This command gives CodeX staff a password "
            "written in the code, and runs only on a developer's machine or in CI.",
        )
