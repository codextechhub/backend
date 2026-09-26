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


#: Keys whose messages speak for the whole request, so no field name is shown.
_UNNAMED_KEYS = frozenset({'detail', 'non_field_errors', NON_FIELD_ERRORS})

#: Sentences shown in ``message`` before the rest are summarised as a count.
_MAX_MESSAGE_PARTS = 5

_REQUEST_ERROR_FALLBACK = (
    "An error occurred. Check the error details for more information."
)


def _error_leaves(data, path=''):
    """Yield ``(path, sentence)`` for every message in a DRF error body.

    DRF nests errors the way the serializer nests fields: a dict per
    serializer, a list per field, and a list of dicts for a ``many=True``
    child. The path is dotted (``lines.1.amount``) so a nested failure still
    says where it is; the unnamed keys add nothing to it.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            key = str(key)
            if key in _UNNAMED_KEYS:
                child = path
            else:
                child = f'{path}.{key}' if path else key
            yield from _error_leaves(value, child)
    elif isinstance(data, (list, tuple)):
        for index, item in enumerate(data):
            if isinstance(item, (dict, list, tuple)):
                yield from _error_leaves(item, f'{path}.{index}' if path else str(index))
            else:
                yield from _error_leaves(item, path)
    elif data is not None and str(data).strip():
        yield path, str(data)


def _request_error_message(data) -> str:
    """The sentence for ``message`` on a DRF error, built from its own detail.

    Every client shows ``message`` and few read ``error.detail``, so a
    serializer's field error has to reach the headline or it reaches nobody:
    a school uploading a mislabelled logo would otherwise read "An error
    occurred" and never learn the file is not the PNG its name claims.

    A body that carries its own ``detail`` keeps it as the headline, as a
    ``NotFound`` or ``ValidationError({"detail": ..., "code": ...})`` always
    has. Otherwise a single message is shown on its own, since the screen
    that raised it already knows which field it is about. Several are each
    prefixed with their field, so the reader can tell them apart. The generic
    sentence is left only for a body with no message in it at all.
    """
    if isinstance(data, dict) and 'detail' in data:
        # The raiser chose this headline; sibling keys (a machine ``code``) are not copy.
        data = data['detail']
    leaves = list(_error_leaves(data))
    if not leaves:
        return _REQUEST_ERROR_FALLBACK
    if len(leaves) == 1:
        return leaves[0][1]
    parts = [f'{path}: {text}' if path else text for path, text in leaves]
    shown = parts[:_MAX_MESSAGE_PARTS]
    hidden = len(parts) - len(shown)
    if hidden:
        shown.append(f'and {hidden} more')
    return '; '.join(shown)


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
        data = response.data
        return Response({
            "success": False,
            "message": _request_error_message(data),
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