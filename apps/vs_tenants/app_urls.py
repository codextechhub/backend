"""Where a tenant's own app is served.

The school app is served per school off wildcard DNS: Corona's people sign in
at ``corona.xvs.codexng.com``, Bright Star's at ``bright-star.xvs.codexng.com``,
and the same bundle answers both. The slug is not stored as a URL anywhere; it
is inserted into one configured host at call time, so a new school needs no
configuration at all.

``SCHOOL_APP_BASE_URL`` carries scheme and host only, and is deliberately NOT
``FRONTEND_BASE_URL``: that one addresses the Console, where staff sign in, and
sending a school's own people there lands them in a backoffice they cannot open.

Read at call time rather than at import, because the deployment rendering the
value is the one that knows its own domain. In development the same insertion
turns ``http://localhost:5174`` into ``http://corona.localhost:5174``, which is
the shape the onboarding seeder already prints.
"""

from urllib.parse import urlsplit, urlunsplit

from django.conf import settings


def school_app_url(slug: str) -> str:
    """The address a school's own users sign in at, or "" when unresolvable.

    Returns "" rather than a half-built address for a missing slug or a missing
    setting. A blank is a thing a caller can test and a screen can leave out; a
    URL that is wrong in a way nobody notices is worse than no URL, because the
    person clicking it believes it.
    """
    slug = (slug or "").strip().lower()
    if not slug:
        return ""

    base = str(getattr(settings, "SCHOOL_APP_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        return ""

    parts = urlsplit(base)
    # A host with no scheme cannot be given a subdomain safely: urlsplit reads
    # "xvs.codexng.com" as a path, so prefixing it would produce "corona.".
    if not parts.netloc:
        return ""

    return urlunsplit((parts.scheme, f"{slug}.{parts.netloc}", parts.path, "", ""))
