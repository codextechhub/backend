# vs_users/token.py
# ---------------------------------------------------------------------------
# Custom JWT token configuration for CodeX Vision.
#
# Extends SimpleJWT's default token to embed school_id, branch_id,
# user_type, and account_status directly into every access token payload.
#
# This means the frontend and any middleware can read the user's workspace
# context directly from the token without hitting the database on every request.
#
# Wired into settings/base.py via:
#   SIMPLE_JWT = {
#       'TOKEN_OBTAIN_SERIALIZER': 'vs_users.token.CustomTokenObtainPairSerializer',
#   }
# ---------------------------------------------------------------------------

from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken


# ---------------------------------------------------------------------------
# Custom RefreshToken
# Adds platform-specific claims to the JWT payload at token generation time.
# Used by LoginService and ActivationService when issuing tokens.
# ---------------------------------------------------------------------------
class CodeXRefreshToken(RefreshToken):

    @classmethod
    def for_user(cls, user):
        """
        Overrides the default for_user() to inject custom claims into
        both the access token and the refresh token payload.

        Custom claims added:
          - user_type      : The user's role category (VISION_STAFF, SCHOOL_ADMIN, etc.)
          - school_id : UUID of the user's school (null for Vision Staff)
          - branch_id      : UUID of the user's branch (null for Admins and Vision Staff)
          - account_status : Current account status (ACTIVE, LOCKED, etc.)
          - full_name      : User's display name for immediate frontend use

        These claims are embedded in the access token so the frontend can
        route the user to the correct workspace without an extra API call.
        """
        token = super().for_user(user)

        # Embed school and branch context
        token['user_type']      = user.user_type
        token['school_id'] = str(user.school_id) if user.school_id else None
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
        """
        data = super().validate(attrs)

        # Append user context to the token response
        data['user'] = {
            'id':             str(self.user.id),
            'email':          self.user.email,
            'full_name':      self.user.full_name,
            'user_type':      self.user.user_type,
            'account_status': self.user.status,
            'school_id': str(self.user.school_id) if self.user.school_id else None,
            'branch_id':      str(self.user.branch_id) if self.user.branch_id else None,
        }

        return data


# ---------------------------------------------------------------------------
# Custom TokenObtainPairView
# Optional — only needed if you expose the standard SimpleJWT /token/ endpoint.
# The vs_users LoginView handles authentication directly via LoginService,
# so this view is registered but not the primary login path.
# ---------------------------------------------------------------------------
class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer