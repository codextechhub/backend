"""Tabular exports for finance reports — CSV, Excel (xlsx) and PDF.

A report is reduced to a neutral :class:`ReportTable` (title + column headers + rows,
with optional bold *summary* rows for totals); three renderers turn that into bytes.
Keeping the table model separate from the renderers means a new report only has to
describe its columns once, and a new format only has to be written once.

Money is rendered as human-facing naira strings in exports (the JSON API carries the
exact kobo); callers pass already-formatted strings in the cells.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

#: Supported export formats → (extension, MIME content type).
EXPORT_FORMATS = {
    "csv": ("csv", "text/csv"),
    "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "pdf": ("pdf", "application/pdf"),
}


@dataclass
class ReportTable:
    """A renderer-neutral rectangular report.

    ``rows`` and ``summary_rows`` are lists of cells (str/number); ``summary_rows`` are
    rendered emphasised (bold) and separated, for totals lines. ``subtitle`` is an
    optional second heading line (e.g. the entity + period).
    """

    title: str
    columns: list[str]
    rows: list[list] = field(default_factory=list)
    summary_rows: list[list] = field(default_factory=list)
    subtitle: str = ""

    @property
    def all_rows(self) -> list[list]:
        return list(self.rows) + list(self.summary_rows)


def to_csv(table: ReportTable) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([table.title])
    if table.subtitle:
        writer.writerow([table.subtitle])
    writer.writerow([])
    writer.writerow(table.columns)
    for row in table.rows:
        writer.writerow(row)
    if table.summary_rows:
        writer.writerow([])
        for row in table.summary_rows:
            writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def to_xlsx(table: ReportTable) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = (table.title[:28] or "Report")

    bold = Font(bold=True)
    ws.append([table.title])
    ws["A1"].font = bold
    if table.subtitle:
        ws.append([table.subtitle])
    ws.append([])

    header_row = ws.max_row + 1
    ws.append(table.columns)
    for cell in ws[header_row]:
        cell.font = bold

    for row in table.rows:
        ws.append(row)
    for row in table.summary_rows:
        ws.append(row)
        for cell in ws[ws.max_row]:
            cell.font = bold

    # Roughly autosize columns to their widest cell.
    for idx, _col in enumerate(table.columns, start=1):
        from openpyxl.utils import get_column_letter
        letter = get_column_letter(idx)
        widest = max(
            [len(str(table.columns[idx - 1]))]
            + [len(str(r[idx - 1])) for r in table.all_rows if idx - 1 < len(r)]
            or [10]
        )
        ws.column_dimensions[letter].width = min(max(widest + 2, 10), 48)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def to_pdf(table: ReportTable) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(A4), title=table.title)
    styles = getSampleStyleSheet()
    story = [Paragraph(table.title, styles["Title"])]
    if table.subtitle:
        story.append(Paragraph(table.subtitle, styles["Normal"]))
    story.append(Spacer(1, 12))

    data = [table.columns] + [[str(c) for c in r] for r in table.all_rows]
    pdf_table = Table(data, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
    ]
    # Bold the summary rows at the bottom.
    n_summary = len(table.summary_rows)
    if n_summary:
        first = len(data) - n_summary
        style.append(("FONTNAME", (0, first), (-1, -1), "Helvetica-Bold"))
        style.append(("LINEABOVE", (0, first), (-1, first), 0.8, colors.black))
    pdf_table.setStyle(TableStyle(style))
    story.append(pdf_table)

    doc.build(story)
    return out.getvalue()


_RENDERERS = {"csv": to_csv, "xlsx": to_xlsx, "pdf": to_pdf}


def render(table: ReportTable, fmt: str) -> tuple[bytes, str, str]:
    """Render ``table`` in ``fmt`` → ``(body, content_type, file_extension)``.

    Raises :class:`ValueError` for an unsupported format (the view turns this into a
    400). The filename is the caller's concern; only the extension is returned here.
    """
    fmt = (fmt or "").lower()
    if fmt not in _RENDERERS:
        raise ValueError(
            f"Unsupported export format '{fmt}'. Choose one of: {', '.join(EXPORT_FORMATS)}."
        )
    extension, content_type = EXPORT_FORMATS[fmt]
    return _RENDERERS[fmt](table), content_type, extension
