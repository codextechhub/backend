"""A conversation as a CSV file, for a participant who holds the export key.

**The file is what the person may read.** Only the messages the caller can see
are written (a member added to a group later reads from their joining point, in
the file as on the screen), and each column follows the Field Access switch of
what it carries: ``sent_at`` the Sent times switch, ``sender`` People in the
conversation, ``message`` the Message switch. A column the caller may not read
is left out rather than written blank, because an empty column would still say
there is something in it. A caller who may read none of the three has nothing to
download.

**Reading order.** Messages run oldest first, each date written in the school's
own date format and clock (``vs_config.display``). A cell that would open as a
formula in a spreadsheet, because it begins with ``=``, ``+``, ``-``, ``@``, a
tab or a carriage return, is written with a leading apostrophe so it stays text.

Nothing here writes the file's contents anywhere but the response: the audit
event records that the export happened and how many messages it held, never
what they said.
"""
from __future__ import annotations

import csv
import io

from vs_config.display import format_datetime

from . import inbox, visibility

#: ``(header, field name)`` for each column, in file order. The field name is
#: the registered Field Access name that governs the column.
COLUMNS = (
    ("sent_at", "sent_at"),
    ("sender", "participants"),
    ("message", "body"),
)

#: How a column is named to the person refused it.
LABELS = {
    "sent_at": "Sent times",
    "participants": "People in the conversation",
    "body": "Message",
}

_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    """*value* as text a spreadsheet will not run as a formula."""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_LEADERS) else text


def readable_columns(may) -> list[tuple[str, str]]:
    """The columns the caller may read; *may* answers for a field name."""
    return [(header, field) for header, field in COLUMNS if may(field)]


def filename_for(conversation) -> str:
    return f"conversation-{conversation.pk}.csv"


def build(tenant, access, columns) -> tuple[bytes, int]:
    """``(csv bytes, message count)`` for the messages *access* may read, in *columns*."""
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow([header for header, _ in columns])
    count = 0
    rows = visibility.visible_messages(access).select_related("sender").order_by("id")
    for message in rows.iterator(chunk_size=1000):
        cells = {
            "sent_at": format_datetime(message.created_at, tenant),
            "participants": inbox.person_name(message.sender),
            "body": message.body,
        }
        writer.writerow([safe_cell(cells[field]) for _, field in columns])
        count += 1
    return out.getvalue().encode("utf-8"), count

