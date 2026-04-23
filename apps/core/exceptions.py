# core/exceptions.py

from rest_framework.views import exception_handler
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework import status
from rest_framework.response import Response


def custom_exception_handler(exc, context):

    # Let DRF handle it first
    response = exception_handler(exc, context)

    # Intercept SimpleJWT token errors
    if isinstance(exc, (InvalidToken, TokenError)):
        return Response({
            "success": False,
            "message": "Authentication failed. Your session token is invalid or has expired.",
            "error": {
                "code": "TOKEN_INVALID",
                "detail": str(exc.detail.get("detail", "Token error")) 
                          if hasattr(exc, "detail") else str(exc)
            }
        }, status=status.HTTP_401_UNAUTHORIZED)

    # Handle all other DRF exceptions
    if response is not None:
        return Response({
            "success": False,
            "message": response.data.get("detail", "An error occurred."),
            "error": {
                "code": "REQUEST_ERROR",
                "detail": response.data
            }
        }, status=response.status_code)

    return response