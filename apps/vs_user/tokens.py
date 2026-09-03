"""Custom JWT token configuration for CodeX Vision.

Extends SimpleJWT's default token to embed tenant_id, tenant_slug, branch_id,
account_status, and full_name directly into every access token payload.

This means the frontend and any middleware can read the user's workspace
context directly from the token without hitting the database on every request.

Wired into settings/base.py via:
  SIMPLE_JWT = {
      'TOKEN_OBTAIN_SERIALIZER': 'vs_user.tokens.CustomTokenObtainPairSerializer',
  }
"""
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken


# ---------------------------------------------------------------------------
# Custom RefreshToken
# Adds platform-specific claims to the JWT payload at token generation time.
# Used by LoginService when issuing tokens.
# ---------------------------------------------------------------------------
class CodeXRefreshToken(RefreshToken):

    @classmethod
    def for_user(cls, user):
        """
        Overrides the default for_user() to inject custom claims into
        both the access token and the refresh token payload.

        Custom claims added:
          - tenant_id      : Primary key of the user's home tenant, as a string
          - tenant_slug    : Slug of that tenant (the stable public identifier)
          - branch_id      : Primary key of the user's branch, as a string
                             (null when the user has no branch)
          - account_status : Current account status (ACTIVE, LOCKED, etc.)
          - full_name      : User's display name for immediate frontend use

        These claims are embedded in the access token so the frontend can
        route the user to the correct workspace without an extra API call.
        """
        token = super().for_user(user)

        # Embed the canonical tenant assertion.
        token['tenant_id'] = str(user.tenant_id)
        token['tenant_slug'] = user.tenant.slug
        token['branch_id']      = str(user.branch_id) if user.branch_id else None
        token['account_status'] = user.status
        token['full_name']      = user.full_name

        return token


# ---------------------------------------------------------------------------
# Custom TokenObtainPairSerializer
# Used by the standard SimpleJWT token obtain view (if you ever use it directly).
# For vs_user, the LoginService calls CodeXRefreshToken.for_user() directly,
# but this serializer is registered in settings for completeness and for any
# future use of the standard /token/ endpoint.
# ---------------------------------------------------------------------------
class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):

    token_class = CodeXRefreshToken

    @classmethod
    def get_token(cls, user):
        """
        Delegates to CodeXRefreshToken.for_user() so the same custom claims
        are applied regardless of whether the token is issued via the
        LoginService or the standard SimpleJWT obtain endpoint.
        """
        return cls.token_class.for_user(user)

    def validate(self, attrs):
        """
        Runs the standard SimpleJWT validation (credential check, token issue)
        and adds the user object to the response data so the frontend gets
        the user profile alongside the tokens in a single response.

        The response body carries id, email, full_name, tenant_kind,
        account_status, tenant_slug and branch_id. ``tenant_kind`` replaces the
        old ``user_type`` key and answers the only question it was ever asked
        here - which side of the platform boundary this account is on. Like its
        predecessor it is a response-body field only, never a token claim:
        authorization runs through tenant RBAC, not through either of them.
        """
        data = super().validate(attrs)

        # Append user context to the token response
        data['user'] = {
            'id':             str(self.user.id),
            'email':          self.user.email,
            'full_name':      self.user.full_name,
            'tenant_kind':    self.user.tenant.kind,
            'account_status': self.user.status,
            'tenant_slug':    self.user.tenant.slug,
            'branch_id':      str(self.user.branch_id) if self.user.branch_id else None,
        }

        return data


# ---------------------------------------------------------------------------
# Custom TokenObtainPairView
# Optional - only needed if you expose the standard SimpleJWT /token/ endpoint.
# The vs_user LoginView handles authentication directly via LoginService,
# so this view is defined here but not currently routed in any urls.py.
# ---------------------------------------------------------------------------
class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
