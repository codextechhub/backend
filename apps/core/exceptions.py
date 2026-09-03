# core/exceptions.py

import logging

from django.core.exceptions import NON_FIELD_ERRORS
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError, RestrictedError
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


def _blocker_summary(exc) -> tuple[str, dict]:
    """Human phrase + machine detail for a ProtectedError / RestrictedError.

    Returns e.g. ("2 positions", {"vs_user.position": 2}). Only model names and
    counts are exposed - never the blocking rows themselves, which may live
    outside the caller's tenant/entity scope.
    """
    objects = (
        getattr(exc, 'protected_objects', None)
        or getattr(exc, 'restricted_objects', None)
        or ()
    )
    counts: dict = {}
    for obj in objects:
        model = type(obj)
        counts[model] = counts.get(model, 0) + 1

    phrases, detail = [], {}
    for model, count in sorted(counts.items(), key=lambda kv: kv[0]._meta.label):
        meta = model._meta
        label = meta.verbose_name if count == 1 else meta.verbose_name_plural
        phrases.append(f'{count} {str(label).lower()}')
        detail[meta.label_lower] = count

    if not phrases:
        return 'other records', {}
    return ' and '.join(phrases), detail


def _validation_error_detail(exc: DjangoValidationError) -> dict:
    """A Django ValidationError as ``{field: [message, ...]}``.

    Errors raised without a field - and every ``ValidationError("some text")``
    from a service is one - collect under ``NON_FIELD_ERRORS`` (``__all__``),
    which is where Django itself puts them, so the shape is the same either
    way and a caller never has to branch on it.
    """
    if hasattr(exc, 'error_dict'):
        return exc.message_dict
    messages = exc.messages if hasattr(exc, 'messages') else [str(exc)]
    return {NON_FIELD_ERRORS: messages}


def _validation_error_message(detail: dict) -> str:
    """One human sentence naming the fields that failed."""
    parts = []
    for field, messages in detail.items():
        prefix = '' if field == NON_FIELD_ERRORS else f'{field}: '
        parts.extend(f'{prefix}{message}' for message in messages)
    return '; '.join(parts) or 'Validation failed.'


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

    # Intercept Django model/form validation errors.
    #
    # `message_dict` and not `messages`: the latter flattens a field-keyed
    # error into bare sentences, so a full_clean() on a model with eight
    # editable columns answers "This field cannot be blank." without saying
    # which one. `messages` stays the fallback for errors that genuinely have
    # no field, such as a ValidationError raised from a service.
    if isinstance(exc, DjangoValidationError):
        detail = _validation_error_detail(exc)
        return Response({
            "success": False,
            "message": _validation_error_message(detail),
            "error": {"code": "VALIDATION_ERROR", "detail": detail},
        }, status=status.HTTP_400_BAD_REQUEST)

    # A delete blocked by an on_delete=PROTECT or RESTRICT foreign key is the
    # client asking for something the data model forbids, not a server bug, so
    # it carries an actionable message. This branch MUST stay above the
    # IntegrityError one: ProtectedError and RestrictedError subclass it and
    # would otherwise be logged as an opaque 500.
    if isinstance(exc, (ProtectedError, RestrictedError)):
        phrase, detail = _blocker_summary(exc)
        logger.info("Delete blocked by protected references: %s", detail or exc)
        return Response({
            "success": False,
            "message": (
                f"This record cannot be deleted because {phrase} still "
                "reference it. Remove or reassign them first."
            ),
            "error": {"code": "PROTECTED_REFERENCE", "detail": detail},
        }, status=status.HTTP_409_CONFLICT)

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
        fallback = "An error occurred. Check the error details for more information."
        data = response.data
        if isinstance(data, dict):
            message = data.get("detail", fallback)
        elif isinstance(data, list) and len(data) == 1 and isinstance(data[0], str):
            # DRF renders ValidationError("some text") as a bare list, not a
            # dict, and calling .get() on that turns a 400 into a 500.
            message = data[0]
        else:
            message = fallback
        return Response({
            "success": False,
            "message": message,
            "error": {
                "code": "REQUEST_ERROR",
                "detail": data,
            }
        }, status=response.status_code)

    # Non-DRF, non-DB exception - log it and return JSON 500 instead of Django HTML page
    logger.exception("Unhandled exception in request", exc_info=exc)
    return Response({
        "success": False,
        "message": "An unexpected error occurred.",
        "error": {"code": "SERVER_ERROR"},
    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)