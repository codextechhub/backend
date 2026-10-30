"""The routes of two-step sign-in. The rules are in ``services.two_step``.

The three sign-in routes take the ``challenge`` a sign-in answered with
``TWO_STEP_REQUIRED`` or ``TWO_STEP_SET_UP_REQUIRED`` and hold no session
themselves, like the sign-in route: the person is not signed in until one of
them answers with the same payload, and the same cookie, a sign-in does.
"""
from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.response import error_response, success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from ..account_scope import administrable_user
from ..browser_session import enforce_browser_origin, is_native_app
from ..services import two_step
from ..services.auth import LoginService
from .auth import sign_in_refused, signed_in_response


class ChallengeSerializer(serializers.Serializer):
    challenge = serializers.CharField(max_length=128)


class ChallengeCodeSerializer(ChallengeSerializer):
    code = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")
    recovery_code = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")


class CodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=16)


def _refused(exc: two_step.TwoStepRefused):
    # A wrong code is the caller's mistake, not a missing sign-in: 400, so no
    # client takes it for an expired session and tries to renew one.
    return error_response(
        message=exc.detail, error={"code": exc.code, "detail": exc.detail},
        status=status.HTTP_400_BAD_REQUEST,
    )


class _SignInStep(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "two_step"
    throttle_classes = [ScopedRateThrottle]

    def finish(self, request, passed, **extra):
        try:
            result = LoginService.finish_two_step(passed, request=request)
        except ValueError as exc:
            return sign_in_refused(exc)
        result.update(extra)
        return signed_in_response(request, result, native=is_native_app(request))


class TwoStepVerifyView(_SignInStep):
    """POST /user/auth/two-step/verify/

    Body ``{challenge, code}``, or ``{challenge, recovery_code}`` for a backup
    code. Answers as a sign-in does. 400 ``TWO_STEP_CODE_INVALID`` for a wrong
    code; ``TWO_STEP_CHALLENGE_INVALID`` once the challenge has expired or had
    five wrong codes, when the person signs in again.

    docstring-name: Enter a two-step sign-in code
    """

    def post(self, request):
        enforce_browser_origin(request)
        ser = ChallengeCodeSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)
        try:
            passed = two_step.verify(
                ser.validated_data["challenge"],
                code=ser.validated_data["code"],
                recovery_code=ser.validated_data["recovery_code"],
                request=request,
            )
        except two_step.TwoStepRefused as exc:
            return _refused(exc)
        return self.finish(request, passed)


class TwoStepSetUpView(_SignInStep):
    """POST /user/auth/two-step/set-up/

    Body ``{challenge}`` from a ``TWO_STEP_SET_UP_REQUIRED`` sign-in. Answers
    ``{secret, otpauth_uri, issuer, account}``: the QR code to scan, and the
    secret to type where a camera is not to hand. Asking again replaces it.

    docstring-name: Start setting up an authenticator
    """

    def post(self, request):
        enforce_browser_origin(request)
        ser = ChallengeSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)
        try:
            data = two_step.start_set_up(ser.validated_data["challenge"])
        except two_step.TwoStepRefused as exc:
            return _refused(exc)
        return success_response(message="Scan the QR code with your authenticator app.", data=data)


class TwoStepSetUpConfirmView(_SignInStep):
    """POST /user/auth/two-step/set-up/confirm/

    Body ``{challenge, code}``: the first code from the new authenticator.
    Answers as a sign-in does, plus ``recovery_codes``, the ten backup codes,
    shown this once.

    docstring-name: Finish setting up an authenticator
    """

    def post(self, request):
        enforce_browser_origin(request)
        ser = ChallengeCodeSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)
        try:
            passed = two_step.confirm_set_up(
                ser.validated_data["challenge"], ser.validated_data["code"], request=request,
            )
        except two_step.TwoStepRefused as exc:
            return _refused(exc)
        return self.finish(request, passed, recovery_codes=passed.recovery_codes)


class MyTwoStepView(APIView):
    """GET /user/auth/me/two-step/

    ``{set_up, required, recovery_codes_left}`` for the person signed in.

    docstring-name: My two-step sign-in
    """

    permission_classes = [IsAuthenticatedAndActive]
    tenant_param_required = False
    pending_tenant_surface = True

    def get(self, request):
        return success_response(data=two_step.status_of(request.user))


class MyTwoStepRecoveryCodesView(APIView):
    """POST /user/auth/me/two-step/recovery-codes/

    Body ``{code}`` from the person's authenticator app. Replaces every backup
    code with ten new ones, shown this once.

    docstring-name: Renew my backup codes
    """

    permission_classes = [IsAuthenticatedAndActive]
    tenant_param_required = False
    pending_tenant_surface = True
    throttle_scope = "two_step"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request):
        ser = CodeSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(message="Invalid request.", error=ser.errors)
        try:
            codes = two_step.renew_recovery_codes(
                request.user, ser.validated_data["code"], request=request,
            )
        except two_step.TwoStepRefused as exc:
            return _refused(exc)
        return success_response(message="New backup codes made.", data={"recovery_codes": codes})


def reset_for(request, user):
    """Reset ``user``'s two-step sign-in for the administrator asking, never their own."""
    if user.pk == request.user.pk:
        return error_response(
            message="You cannot reset your own two-step sign-in. Ask another administrator.",
            error={"code": "CANNOT_ACT_ON_SELF"},
            status=status.HTTP_403_FORBIDDEN,
        )
    had_one = two_step.reset(user, actor=request.user, request=request)
    return success_response(
        message="Two-step sign-in reset. They will set it up again at their next sign-in.",
        data={"had_authenticator": had_one},
    )


class UserTwoStepResetView(APIView):
    """POST /user/{user_id}/two-step/reset/

    Clears someone's authenticator and backup codes, for a person who has lost
    their phone. They set up again at their next sign-in. Never the caller's
    own: 403 ``CANNOT_ACT_ON_SELF``.

    Permission: IsAuthenticatedAndActive, HasRBACPermission
    RBAC: platform.team.update

    docstring-name: Reset a person's two-step sign-in
    """

    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = "platform.team.update"

    def post(self, request, user_id):
        user = administrable_user(request, user_id)
        if user is None:
            return error_response(message="User not found.", status=status.HTTP_404_NOT_FOUND)
        return reset_for(request, user)
