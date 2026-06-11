# core/exceptions.py

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

logger = logging.getLogger('core.exceptions')


def _is_unique_violation(exc: IntegrityError) -> bool:
    """True when the IntegrityError is a UNIQUE-constraint violation.

    Engine-aware: PostgreSQL exposes SQLSTATE 23505 on the driver exception,
    MySQL/MariaDB use error code 1062, SQLite spells it out in the message.
    """
    cause = exc.__cause__
    # PostgreSQL (psycopg2/psycopg3): SQLSTATE 23505 = unique_violation
    sqlstate = getattr(cause, 'pgcode', None) or getattr(
        getattr(cause, 'diag', None), 'sqlstate', None
    )
    if sqlstate == '23505':
        return True
    # MySQL / MariaDB: (1062, "Duplicate entry ...")
    args = getattr(cause, 'args', None) or exc.args
    if args and args[0] == 1062:
        return True
    # SQLite and fallback
    text = str(exc).lower()
    return 'unique constraint' in text or 'duplicate entry' in text


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

    # Intercept Django model/form validation errors (args[0] is a list, not a dict)
    if isinstance(exc, DjangoValidationError):
        messages = exc.messages if hasattr(exc, 'messages') else [str(exc)]
        return Response({
            "success": False,
            "message": '; '.join(messages),
            "error": {"code": "VALIDATION_ERROR", "detail": messages},
        }, status=status.HTTP_400_BAD_REQUEST)

    # Intercept DB integrity violations. ONLY unique violations are the
    # client's fault ("already exists"); FK / NOT NULL / CHECK violations are
    # server-side bugs and must surface as logged 500s, not fake duplicates.
    if isinstance(exc, IntegrityError):
        if _is_unique_violation(exc):
            return Response({
                "success": False,
                "message": "A record with these details already exists.",
                "error": {"code": "DUPLICATE"},
            }, status=status.HTTP_400_BAD_REQUEST)
        logger.exception("Non-unique IntegrityError in request", exc_info=exc)
        return Response({
            "success": False,
            "message": "An unexpected error occurred.",
            "error": {"code": "SERVER_ERROR"},
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Handle typed domain exceptions from any app (duck-typed: error_code + message attributes)
    if hasattr(exc, 'error_code') and hasattr(exc, 'message'):
        return Response({
            "success": False,
            "message": exc.message,
            "error": {"code": exc.error_code, "detail": getattr(exc, 'extra', {}) or {}},
        }, status=getattr(exc, 'http_status', status.HTTP_422_UNPROCESSABLE_ENTITY))

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