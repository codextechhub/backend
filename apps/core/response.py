# core/response.py

from rest_framework.response import Response


def success_response(message="", data=None, status=200):
    """The standard success envelope.

    ``message`` defaults to empty rather than being required. It was
    positional-only-by-necessity, and a list view that has nothing to announce
    naturally reads ``success_response(data=...)`` - which raised TypeError at
    request time and surfaced as a 500 with code SERVER_ERROR. Three endpoints
    shipped that way. A missing message is a blank message, never a crash.
    """
    return Response({
        "success": True,
        "message": message,
        # An empty list stays a list; only a genuinely absent payload becomes {}.
        # `data or {}` collapses the empty list into an object, so a non-paginated
        # list endpoint answers {} when it has nothing to show and any caller doing
        # data.map(...) crashes on the empty case.
        "data": {} if data is None else data
    }, status=status)


def error_response(message, error=None, status=400, code=None):
    body = {
        "success": False,
        "message": message,
        "error": error or {}
    }
    if code is not None:
        body["code"] = code
    return Response(body, status=status)