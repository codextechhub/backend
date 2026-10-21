"""The send limit: how fast one person may write in messaging.

One budget (``message_send``, 20 a minute) covers every route that can store a
message, on every surface: the staff start, send, group, broadcast and
broadcast-reply routes, and the parent and student start and send routes. It
is keyed by the signed-in person and their school, so Mrs. Funke Adeyemi's
sends never use up anybody else's.

What is counted is a ``POST`` to one of those routes. A start is counted
whether or not it carries a first message: finding the conversation that
already exists is a ``POST`` too, and one budget unit for it is cheaper than
reading the body twice. A request refused for another reason (a validation
error, a missing key) still counts, because the throttle runs before the view.

Reads are never counted. The inbox, a thread and the unread badge are polled
every few seconds by every open screen, so counting them would lock out someone
who is only reading. Every method other than ``POST`` passes the throttle
untouched and leaves no record, and the routes that do not store a message
(marking a thread read, the contact lists) do not carry it at all.

The 429 is the module's own refusal, :class:`MessageRateLimited`, so the shared
exception handler renders the code and the sentence a composer can show, with
the seconds to wait in ``error.detail.retry_after``.
"""
import math

from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle

from .exceptions import MessageRateLimited


class MessageSendThrottle(SimpleRateThrottle):
    """Counts the ``POST`` requests of one person at one school against ``message_send``."""

    scope = "message_send"

    def allow_request(self, request, view):
        if request.method != "POST":
            return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        tenant = getattr(request, "tenant", None)
        ident = f"{getattr(tenant, 'pk', 0)}:{user.pk}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


class SendLimited:
    """Mixin for a view with a route that stores a message.

    Adds :class:`MessageSendThrottle` beside whatever throttles the project
    applies by default, so a view that also names a ``throttle_scope`` keeps that
    behaviour, and answers a refused send with :class:`MessageRateLimited`.
    """

    throttle_classes = [*api_settings.DEFAULT_THROTTLE_CLASSES, MessageSendThrottle]

    def throttled(self, request, wait):
        raise MessageRateLimited(
            retry_after=None if wait is None else max(1, math.ceil(wait)),
        )
