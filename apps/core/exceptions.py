# core/exceptions.py

import logging

from django.db import IntegrityError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

logger = logging.getLogger('core.exceptions')


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
                          if hasattr(exc, "detail") else str(exc),
            }
        }, status=status.HTTP_401_UNAUTHORIZED)

    # Intercept DB integrity violations (unique constraint, FK, not-null, etc.)
    if isinstance(exc, IntegrityError):
        return Response({
            "success": False,
            "message": "A record with these details already exists.",
            "error": {"code": "DUPLICATE"},
        }, status=status.HTTP_400_BAD_REQUEST)

    # Handle all other DRF exceptions
    if response is not None:
        return Response({
            "success": False,
            "message": response.data.get("detail", "An error occurred. Check the error details for more information."),
            "error": {
                "code": "REQUEST_ERROR",
                "detail": response.data,
            }
        }, status=response.status_code)

    # Non-DRF, non-DB exception — log it and return JSON 500 instead of Django HTML page
    logger.exception("Unhandled exception in request", exc_info=exc)
    return Response({
        "success": False,
        "message": "An unexpected error occurred.",
        "error": {"code": "SERVER_ERROR"},
    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)