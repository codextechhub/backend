"""One-line JSON formatting for the platform's log stream.

Structure is what makes a log stream searchable, and searchable is what an
investigation needs. An unstructured line can be read by a person watching it
scroll past; it cannot be filtered to "every failure for Corona Secondary
School in March" a quarter later.

Written by hand rather than pulled in as a dependency: the whole job is
``json.dumps`` over a fixed set of record attributes, and a log formatter is a
poor place to add a package that runs on every single line.

Redaction is NOT done here. It belongs to ``core.redaction.RedactingLogFilter``
on the handler, so it applies to the plain formatter too - a developer running
locally with ``LOG_FORMAT=plain`` gets the same protection as production.
"""
from __future__ import annotations

import json
import logging

#: Standard LogRecord attributes. Anything on a record that is NOT one of
#: these was attached by a caller through ``extra=`` and is worth carrying
#: into the JSON object, which is how a task or tenant id gets into the stream.
_STANDARD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


class JSONFormatter(logging.Formatter):
    """Render a log record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info or record.exc_text:
            # ``exc_text`` first: RedactingLogFilter formats the traceback and
            # caches the SCRUBBED text there. Re-formatting from ``exc_info``
            # would rebuild the original, unredacted string and put the
            # exception message - the part that carries the email address -
            # straight back into the stream.
            payload["exception"] = (
                record.exc_text or self.formatException(record.exc_info)
            )
        if record.stack_info:
            payload["stack"] = record.stack_info

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            # Anything not JSON-native is stringified rather than dropped: a
            # log line that loses a field is worse than one carrying a repr.
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        return json.dumps(payload, default=str)
