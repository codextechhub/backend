"""Rate limits keyed by the random identifier carried on a staff ID card."""

import hashlib

from rest_framework.throttling import SimpleRateThrottle


class CardIdentifierThrottle(SimpleRateThrottle):
    """Bound distributed requests against one card without storing its key in cache."""

    scope = "card_login"

    def get_cache_key(self, request, view):
        raw_identifier = (
            request.query_params.get("card_id")
            or request.data.get("card_id")
            or ""
        )
        identifier = str(raw_identifier).strip().lower()
        if not identifier:
            return None
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}


class CardPreviewIdentifierThrottle(CardIdentifierThrottle):
    """Keep preview reads separate from password attempts on the same card."""

    scope = "card_login_preview"
