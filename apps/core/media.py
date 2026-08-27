"""
Who may read a stored file, and for how long.

Two separate questions, deliberately kept separate, because each one alone
leaves a hole the other closes.

**Whose file is this?**  A ``StoredFile`` used to carry nothing but a name and
some bytes, so ``/media/<name>`` could only ever ask "are you signed in?".  The
name itself was the credential.  A bursar who left Corona Secondary School for
Greenfield Academy still had the Corona URLs in her browser history, and her new
and entirely valid Greenfield session was enough to replay them - the request
even carried ``?tenant=greenfield-academy`` in the query string, and nothing
compared it to anything.  :func:`authorize` now compares the row's tenant to the
caller's, then asks the owning record's own policy whether this caller may read
it.  A parent holding a forwarded link to the payroll import fails the second
check even though they pass the first.

**Is this URL still live?**  Tenant and record checks say nothing about a link
that was correct once and has been sitting in a WhatsApp thread since March.
:func:`signed_url` therefore stamps every URL with an expiry *and the user it
was issued to*, so a forwarded link is dead twice over: expired, and addressed
to somebody else.  Signing alone would not be enough either - inside its window
an unbound signature is still a bearer token - which is why both run.

Registering a policy is how an app opts its files into being servable at all.
There is no default: a file whose owning model has registered nothing is not
served, because the alternative is that adding a ``FileField`` silently
publishes it to every account on the platform.
"""
from __future__ import annotations

import logging
from typing import Callable

from django.conf import settings
from django.core import signing
from django.utils import timezone

logger = logging.getLogger(__name__)

#: The size of the expiry window. A URL lives between one and two of these (see
#: :func:`expiry_bucket`), so the default is a 15 to 30 minute life. Short,
#: because the URL is re-issued whenever the owning record is serialized; long
#: enough that a page somebody is reading does not lose its images mid-scroll.
DEFAULT_TTL_SECONDS = 900

#: Query parameter carrying the signature.
TOKEN_PARAM = "t"

_SALT = "core.media.url.v1"

#: model -> predicate. See :func:`register_policy`.
_POLICIES: dict[type, Callable] = {}


def ttl_seconds() -> int:
    return int(getattr(settings, "MEDIA_SIGNED_URL_TTL_SECONDS", DEFAULT_TTL_SECONDS))


# --------------------------------------------------------------------------- #
# Policies                                                                     #
# --------------------------------------------------------------------------- #
def register_policy(model, predicate: Callable) -> None:
    """Declare who may read files owned by ``model``.

    ``predicate(request, owner) -> bool`` is called only after the caller has
    already cleared authentication, signature and tenant, so it is answering the
    narrow question the tenant check cannot: *within this school*, may this
    person see this record?  Corona's bursar and Corona's parents both pass the
    tenant check; only one of them should get the expense receipt.

    Each owning app registers its own policies from its ``AppConfig.ready()``,
    so the engines never import the school apps to find out.
    """
    _POLICIES[model] = predicate


def policy_for(model):
    """Return the registered predicate for ``model``, walking up to its bases.

    Concrete inheritance (``FinanceDocument`` and friends) means the model a
    row is bound to may be a subclass of the one that registered.
    """
    for candidate in model.__mro__:
        if candidate in _POLICIES:
            return _POLICIES[candidate]
    return None


# --------------------------------------------------------------------------- #
# Signing                                                                      #
# --------------------------------------------------------------------------- #
def _current_user():
    """The user this request is acting as, without threading a request around.

    Every call site that emits a media URL is inside a request, but most reach
    the file through a model relation rather than a serializer context, so
    plumbing a ``request`` to all of them would be a much larger change than the
    one this fixes. The effective user is already in context for auditing.
    """
    from vs_tenants.context import get_current_audit_identity

    _actor, effective, _session = get_current_audit_identity()
    return effective


def expiry_bucket(*, now=None) -> int:
    """The expiry stamp every URL minted in this window shares.

    Rounding matters more than it looks. A signature over the exact moment of
    minting makes the URL different in every response, and the browser caches by
    full URL - so the school's crest would be re-downloaded on every screen that
    serialized it, and two payloads describing the same file would disagree about
    its address. Bucketing makes the URL stable for the window and still finite.

    The stamp is one to two windows ahead, never less than one: rounding to the
    next boundary alone would hand out a URL with seconds left on it to anyone
    unlucky enough to ask just before the turn.
    """
    ttl = max(ttl_seconds(), 1)
    stamp = int((now or timezone.now()).timestamp())
    return (stamp // ttl + 2) * ttl


def sign(name: str, user, *, exp: int | None = None) -> str:
    """Mint the token that makes ``name`` fetchable by ``user`` until ``exp``.

    ``Signer``, not ``TimestampSigner``: the expiry is carried in the payload we
    control rather than in a stamp the signer writes at call time, which is what
    keeps the same file's URL identical inside a window. ``exp`` is injectable so
    a test can mint an already-dead one without waiting.
    """
    payload = {
        "n": name,
        "u": str(user.pk),
        "e": int(exp if exp is not None else expiry_bucket()),
    }
    return signing.Signer(salt=_SALT).sign_object(payload)


def signed_url(name: str, *, user=None, absolute_for=None) -> str:
    """Return the fetchable URL for a stored file, or ``""`` when there is none.

    The identity is resolved in the order it is most likely to be right: an
    explicit ``user``, then the ``absolute_for`` request's own user, then the
    effective user in context. The middle step matters more than it looks - a
    caller holding a request but running outside the JWT authenticator (DRF's
    ``force_authenticate``, an internal render) has an identity that never
    reached the context, and without it the URL would come back empty and the
    image would simply be missing.

    Pass ``absolute_for=request`` where the caller needs a fully-qualified URL.
    """
    if not name:
        return ""
    if user is None and absolute_for is not None:
        user = getattr(absolute_for, "user", None)
    if user is None or not getattr(user, "is_authenticated", True):
        user = _current_user()
    if user is None or not getattr(user, "pk", None):
        # No identity to bind to. Emitting an unsigned URL here would quietly
        # reintroduce the bearer-token behaviour, so emit nothing instead.
        return ""
    query = f"{TOKEN_PARAM}={sign(name, user)}"
    # The tenant assertion rides along. Authentication requires it on this route
    # like any other, and the thing consuming this URL is an <img src> or a
    # download link the frontend never gets to rewrite - so a URL that needs a
    # second parameter bolted on before it works is a URL that does not work.
    slug = _current_tenant_slug(user, absolute_for)
    if slug:
        query = f"{query}&tenant={slug}"
    url = f"{settings.MEDIA_URL}{name}?{query}"
    return absolute_for.build_absolute_uri(url) if absolute_for is not None else url


def _current_tenant_slug(user, request=None) -> str:
    """The tenant this URL is being read under, preferring the asserted one.

    The request's tenant, not the user's home tenant: they differ exactly when a
    CodeX engineer is impersonating a school to see what its staff see, and the
    URL has to work in that session too.
    """
    from vs_tenants.context import get_current_tenant

    tenant = getattr(request, "tenant", None) or get_current_tenant()
    if tenant is None:
        tenant = getattr(user, "tenant", None)
    return getattr(tenant, "slug", "") or ""


def signature_ok(token: str, name: str, user) -> bool:
    if not token or user is None or not getattr(user, "pk", None):
        return False
    try:
        payload = signing.Signer(salt=_SALT).unsign_object(token)
    except signing.BadSignature:
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("n") != name or payload.get("u") != str(user.pk):
        return False
    try:
        expires_at = int(payload.get("e", 0))
    except (TypeError, ValueError):
        return False
    return expires_at > int(timezone.now().timestamp())


# --------------------------------------------------------------------------- #
# Authorisation                                                                #
# --------------------------------------------------------------------------- #
def authorize(request, row) -> bool:
    """Whether ``request`` may read ``row``. Fail closed at every step."""
    if row.revoked_at is not None:
        return False

    token = (request.GET.get(TOKEN_PARAM) or "").strip()
    if not signature_ok(token, row.name, getattr(request, "user", None)):
        return False

    # Whose file is it. A row with no tenant was written outside any request
    # (a command, a scheduled job); those are served by their own views, never
    # here, so "unknown owner" is refused rather than shared.
    caller_tenant = getattr(request, "tenant", None)
    if row.tenant_id is None or caller_tenant is None:
        return False
    if str(row.tenant_id) != str(caller_tenant.pk):
        return False

    # Which record it is evidence for.
    if row.owner_content_type_id is None or not row.owner_object_id:
        return False
    from django.contrib.contenttypes.models import ContentType

    # get_for_id is cached; ``row.owner_content_type`` would be a query on every
    # image the page loads.
    model = ContentType.objects.get_for_id(row.owner_content_type_id).model_class()
    if model is None:
        return False
    predicate = policy_for(model)
    if predicate is None:
        logger.warning(
            "core.media: %s owns stored files but registers no read policy; "
            "refusing to serve %s", model.__name__, row.name,
        )
        return False
    # ``_default_manager`` rather than ``objects``: a model whose default manager
    # hides archived rows should hide their evidence too, and a model that names
    # its manager something else still resolves.
    owner = model._default_manager.filter(pk=row.owner_object_id).first()
    if owner is None:
        return False
    try:
        return bool(predicate(request, owner))
    except Exception:  # pragma: no cover - a broken policy must not open the door
        logger.exception("core.media: read policy for %s raised", model.__name__)
        return False


# --------------------------------------------------------------------------- #
# Revocation                                                                   #
# --------------------------------------------------------------------------- #
def revoke(names) -> int:
    """Close the URL and drop the bytes for ``names``. Returns rows affected.

    Kept as an explicit helper because the platform archives rather than
    deletes: a record that is retired never fires ``post_delete``, so whoever
    retires it has to say that its evidence goes with it.
    """
    from .models import StoredFile

    if isinstance(names, str):
        names = [names]
    names = [n for n in names if n]
    if not names:
        return 0
    return StoredFile.objects.filter(
        name__in=names, revoked_at__isnull=True,
    ).update(revoked_at=timezone.now(), content=b"", size=0)


def revoke_for(owner) -> int:
    """Revoke every stored file bound to ``owner``."""
    from django.contrib.contenttypes.models import ContentType

    from .models import StoredFile

    ct = ContentType.objects.get_for_model(type(owner))
    return StoredFile.objects.filter(
        owner_content_type=ct, owner_object_id=str(owner.pk),
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now(), content=b"", size=0)
