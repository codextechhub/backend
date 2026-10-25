"""Push for the signed-in person: the Web Push key, and their browser and phone devices."""
from __future__ import annotations

import re

from django.conf import settings
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.response import error_response, success_response
from vs_rbac.permissions import TenantSurfaceAllowed

from .models import NativePushDevice
from .services import native_push, push


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

    @staticmethod
    def _owns_session(user, account) -> bool:
        """True when *account* is a device session *user* holds and has not ended."""
        from vs_user.models import LoginSession

        return LoginSession.all_objects.filter(
            user=user, device_key=account, is_active=True,
        ).exists()


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


_APP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


class _DeviceSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=4096)
    platform = serializers.ChoiceField(choices=NativePushDevice.Platform.choices)
    app = serializers.CharField(max_length=100)
    app_version = serializers.CharField(required=False, allow_blank=True, max_length=32)
    account = serializers.CharField(required=False, allow_blank=True, max_length=64)

    def validate_app(self, app):
        if not _APP_ID.match(app):
            raise serializers.ValidationError("Send the app's package or bundle id.")
        return app


class PushDeviceView(_PushView):
    """POST, DELETE /v1/notify/push/devices/

    POST ``{token, platform, app, app_version?, account?}`` remembers this
    phone for the signed-in person: ``token`` is the Firebase token of the
    install, ``platform`` is ``android`` or ``ios`` and ``app`` is the app's
    package or bundle id. Posting it again refreshes it. A device made with an
    ``account`` is deleted when that device session signs out; an ``account``
    the person does not hold is dropped. 503 ``PUSH_NOT_CONFIGURED`` where
    this environment sends no push to phones. DELETE ``{token}`` forgets the
    person's own device for that token and no one else's.

    docstring-name: My push phones
    """

    def post(self, request):
        if not push.phones_configured():
            return self.unavailable()
        writer = _DeviceSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        account = (data.get("account") or "").strip()
        if account and not self._owns_session(request.user, account):
            account = ""
        native_push.register(
            request.user, token=data["token"], platform=data["platform"], app_id=data["app"],
            app_version=data.get("app_version", ""), device_key=account,
        )
        return success_response("Notifications are on for this device.", status=201)

    def delete(self, request):
        token = str((request.data or {}).get("token") or "")
        native_push.unregister(request.user, token)
        return success_response("Notifications are off for this device.")
