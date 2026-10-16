"""Web Push for the signed-in person: the server's key, and their devices."""
from __future__ import annotations

from django.conf import settings
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.response import error_response, success_response
from vs_rbac.permissions import TenantSurfaceAllowed

from .services import push


class _PushView(APIView):
    """Self-scoped: a person's own devices, so open to a tenant not yet live too."""

    permission_classes = [IsAuthenticated, TenantSurfaceAllowed]
    pending_tenant_surface = True

    def unavailable(self):
        return error_response(
            message="Notifications on this device are not available yet.",
            error={"code": "PUSH_NOT_CONFIGURED"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class PushKeyView(_PushView):
    """GET /v1/notify/push/key/

    The public key a device subscribes with (``applicationServerKey``). 503
    ``PUSH_NOT_CONFIGURED`` where this environment sends no push.

    docstring-name: Push key
    """

    def get(self, request):
        if not push.configured():
            return self.unavailable()
        return success_response(data={"public_key": settings.WEB_PUSH_VAPID_PUBLIC_KEY})


class _SubscriptionSerializer(serializers.Serializer):
    endpoint = serializers.URLField(max_length=2000)
    keys = serializers.DictField(child=serializers.CharField(max_length=255))
    account = serializers.CharField(required=False, allow_blank=True, max_length=64)

    def validate_keys(self, keys):
        if not keys.get("p256dh") or not keys.get("auth"):
            raise serializers.ValidationError("Send the device's p256dh and auth keys.")
        return keys

    def validate_endpoint(self, endpoint):
        if not endpoint.startswith("https://"):
            raise serializers.ValidationError("A push endpoint is an https address.")
        return endpoint


class PushSubscriptionView(_PushView):
    """POST, DELETE /v1/notify/push/subscriptions/

    POST ``{endpoint, keys: {p256dh, auth}, account?}`` (the browser's
    ``PushSubscription.toJSON()``, plus the device-session ``account`` when the
    app holds one) remembers this device for the signed-in person; posting it
    again refreshes it. A subscription made with an ``account`` is deleted when
    that device session signs out. DELETE ``{endpoint}`` forgets it.

    docstring-name: My push devices
    """

    def post(self, request):
        if not push.configured():
            return self.unavailable()
        writer = _SubscriptionSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        account = (data.get("account") or "").strip()
        if account and not self._owns_session(request.user, account):
            account = ""
        push.subscribe(
            request.user, endpoint=data["endpoint"], p256dh=data["keys"]["p256dh"],
            auth=data["keys"]["auth"], device_key=account,
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        return success_response("Notifications are on for this device.", status=201)

    def delete(self, request):
        endpoint = str((request.data or {}).get("endpoint") or "")
        push.unsubscribe(request.user, endpoint)
        return success_response("Notifications are off for this device.")

    @staticmethod
    def _owns_session(user, account) -> bool:
        from vs_user.models import LoginSession

        return LoginSession.all_objects.filter(
            user=user, device_key=account, is_active=True,
        ).exists()
