"""Re-encode the oversized cover art carried inside generated .docx files.

Why this exists
---------------
``generate_requirements_documents.py`` builds each new version from an existing
one (``--reference``), so everything the reference carries is inherited. The
cover photo in the first-page header was a 1,865 x 2,413 pixel JPEG weighing
1.28 MB, which is 95% of a 1.37 MB document: the prose only accounts for the
last 60 KB or so. Every version generated since inherited that same 1.28 MB, and
every version generated in future would too.

So this is not a one-off byte swap. Run it over a document and the cover is
re-encoded in place; run it over the reference before generating, and the
document you generate is born small.

What it does, and what it deliberately does not
-----------------------------------------------
* Only JPEG media is touched. The PNG media in these documents are small logos
  with transparency, and re-encoding those as JPEG would flatten it.
* Only media above ``SIZE_THRESHOLD`` is touched, so a small image is never put
  through a lossy round trip for no gain.
* Colour is left alone. The cover is a black and white photograph, so dropping
  its chroma planes looked like free savings; measured, it came to 2%, because
  an optimized JPEG already spends almost nothing on chroma that carries
  nothing. Not worth a "is this image really monochrome" guess that would
  flatten a colour logo the day one appears.
* The replacement is kept only when it is materially smaller, which is also
  what makes the tool safe to run twice. See ``MIN_SAVING``.
* Nothing but the image bytes changes. The name, the extension and therefore
  every ``.rels`` target and the ``[Content_Types].xml`` defaults stay as they
  are, and the on-page size is fixed in EMU by the drawing itself, so the
  document renders identically whatever pixel dimensions we choose.

Resolution
----------
The cover fills an A4 page (8.27 x 11.69 in). ``MAX_EDGE`` of 1,754 pixels is
exactly 150 dpi down the long edge, which is print-grade for a photographic
background and indistinguishable from the 225 dpi original on screen.

Usage
-----
    python docs/frd/tools/shrink_cover_art.py docs/frd
    python docs/frd/tools/shrink_cover_art.py path/to/one.docx --dry-run
"""
from __future__ import annotations

import argparse
import io
import shutil
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

#: Media smaller than this is left alone.
SIZE_THRESHOLD = 200 * 1024

#: Longest edge in pixels after re-encoding. See "Resolution" above.
MAX_EDGE = 1754

#: JPEG quality. 76 was chosen by comparing 1:1 crops of the cover against the
#: original; the difference is not visible, and lower starts to smear the fine
#: window detail that makes up most of the picture.
QUALITY = 76

#: How much smaller the replacement has to be before it is worth having.
#:
#: This is what makes the tool safe to run twice. "Keep it if it is smaller"
#: sounds like the same thing and is not: re-encoding an already-shrunk JPEG
#: shaves a few hundred bytes off it every single time, so the tool would accept
#: its own output forever and put the cover through a fresh generation of lossy
#: compression on each run. A quarter is far above that noise and far below the
#: 74% a genuinely oversized image gives up.
MIN_SAVING = 0.25

MEDIA_PREFIX = "word/media/"
JPEG_SUFFIXES = (".jpg", ".jpeg")


def shrink_image(data: bytes) -> bytes | None:
    """Re-encoded bytes, or None when the original is already the better one."""
    image = Image.open(io.BytesIO(data))
    image.load()
    # RGB regardless of what the source declares: a CMYK or palette JPEG cannot
    # be saved back out as a baseline JPEG without it.
    image = image.convert("RGB")

    longest = max(image.size)
    if longest > MAX_EDGE:
        scale = MAX_EDGE / longest
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.LANCZOS,
        )

    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    shrunk = buffer.getvalue()
    if len(shrunk) > len(data) * (1 - MIN_SAVING):
        return None
    return shrunk


def rewrite_document(path: Path, *, dry_run: bool) -> dict[str, tuple[int, int]]:
    """Re-encode oversized JPEG media in one .docx. Returns what changed."""
    with zipfile.ZipFile(path, "r") as source:
        entries = [(item, source.read(item.filename)) for item in source.infolist()]

    replacements: dict[str, bytes] = {}
    changed: dict[str, tuple[int, int]] = {}
    for item, data in entries:
        name = item.filename
        if not name.startswith(MEDIA_PREFIX):
            continue
        if not name.lower().endswith(JPEG_SUFFIXES):
            continue
        if len(data) < SIZE_THRESHOLD:
            continue
        shrunk = shrink_image(data)
        if shrunk is None:
            continue
        replacements[name] = shrunk
        changed[name] = (len(data), len(shrunk))

    if not replacements or dry_run:
        return changed

    # Written beside the original and moved into place, so an interrupted run
    # cannot leave a half-written document where a valid one used to be. Each
    # entry keeps its own ZipInfo, so ordering and per-entry compression survive.
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".docx", delete=False) as handle:
        temporary_path = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as target:
            for item, data in entries:
                target.writestr(item, replacements.get(item.filename, data))
        shutil.copystat(path, temporary_path)
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return changed


def documents_under(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(target.rglob("*.docx"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path, help="A .docx, or a directory to walk.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    args = parser.parse_args()

    documents = documents_under(args.target)
    if not documents:
        parser.error(f"No .docx found under {args.target}")

    touched = 0
    before_total = 0
    after_total = 0
    for document in documents:
        before = document.stat().st_size
        changed = rewrite_document(document, dry_run=args.dry_run)
        before_total += before
        after_total += before if args.dry_run else document.stat().st_size
        if not changed:
            continue
        touched += 1
        detail = ", ".join(
            f"{name.removeprefix(MEDIA_PREFIX)} {old / 1024:.0f} KB -> {new / 1024:.0f} KB"
            for name, (old, new) in changed.items()
        )
        print(f"{document}: {detail}")

    verb = "would shrink" if args.dry_run else "shrank"
    print(
        f"\n{verb} {touched} of {len(documents)} documents; "
        f"{before_total / 1024 / 1024:.1f} MB -> {after_total / 1024 / 1024:.1f} MB"
    )


if __name__ == "__main__":
    main()
