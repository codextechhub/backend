"""A school site is a branch, in every file this project writes.

The data model calls it ``Branch``, the API returns ``branch``, ``branch_name``
and ``scope_label``, and the screens a school reads say branch. A synonym in
code teaches the next reader the wrong word, and from there it leaks into what
a school sees: a permission group's name, an error message, a seeded record.
The synonym this module refuses is "campus", in any letter case.

Every ``.py``, ``.html``, ``.txt``, ``.md``, ``.json``, ``.yml``, ``.yaml`` and
``.csv`` file under ``apps/`` is read, except in these places:

- ``migrations`` directories. A migration records what the schema and the data
  were, and one that repairs rows stored under the old word has to name it to
  find them.
- ``__pycache__`` directories, which hold compiled bytecode, not source.
- ``STATIC_ROOT`` and ``MEDIA_ROOT``. Both resolve inside ``apps/``, and they
  hold what ``collectstatic`` copies in from installed packages and what people
  upload, so nothing in them is text this project writes.
- This module, which has to spell the word to search for it.

``apps/static/`` is read like any other directory. It is the project's own
``STATICFILES_DIRS`` source and holds only placeholder files the project
commits, not third-party or collected assets, so whatever is added there is
the project's own writing.

One line is allowed, in the development seed: it renames branches that older
runs of the seed stored under the old name, and it has to spell that name to
match those rows. ``ALLOWED_LINES`` records it by path and exact content. Each
entry excuses a single line, so a copy of it still fails, and a second test
fails once the line is gone, so the exception cannot outlive the code it
excuses.
"""
from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

APPS_DIR = Path(__file__).resolve().parent.parent

FORBIDDEN_WORD = "campus"

TEXT_SUFFIXES = frozenset(
    {".py", ".html", ".txt", ".md", ".json", ".yml", ".yaml", ".csv"}
)

SKIPPED_DIR_NAMES = frozenset({"migrations", "__pycache__"})

# Path relative to apps/, and the line with its surrounding whitespace stripped.
ALLOWED_LINES = frozenset({
    (
        "schools/vs_schools/dev/fixtures.py",
        'tenant=tenant, name=f"{name} Main Campus",',
    ),
})


def _skipped_roots() -> set[Path]:
    """The configured directories whose contents this project does not write."""
    return {
        Path(root).resolve()
        for root in (settings.STATIC_ROOT, settings.MEDIA_ROOT)
        if root
    }


def _text_files():
    """Every file the rule applies to, in a stable order."""
    this_module = Path(__file__).resolve()
    skipped_roots = _skipped_roots()
    for dirpath, dirnames, filenames in os.walk(APPS_DIR):
        here = Path(dirpath)
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIPPED_DIR_NAMES
            and (here / name).resolve() not in skipped_roots
        )
        for filename in sorted(filenames):
            path = here / filename
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if path.resolve() != this_module:
                yield path


class BranchVocabularyTests(SimpleTestCase):
    """The module's rule, checked against the files on disk."""

    def test_no_file_calls_a_branch_by_another_name(self):
        """Each allowed line is excused once, so a copy of it still fails."""
        excused = set()
        offenders = []
        for path in _text_files():
            relative = path.relative_to(APPS_DIR).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if FORBIDDEN_WORD not in line.lower():
                    continue
                entry = (relative, line.strip())
                if entry in ALLOWED_LINES and entry not in excused:
                    excused.add(entry)
                    continue
                offenders.append(f"  {relative}:{number}: {line.strip()}")

        if offenders:
            self.fail(
                'A school site is a branch. Write "branch" on these lines '
                "instead:\n" + "\n".join(offenders)
            )

    def test_every_allowed_line_still_exists(self):
        """An entry whose line is gone would excuse the next one written there."""
        stale = []
        for relative, allowed in sorted(ALLOWED_LINES):
            path = APPS_DIR / relative
            lines = (
                path.read_text(encoding="utf-8").splitlines()
                if path.is_file() else []
            )
            if allowed not in {line.strip() for line in lines}:
                stale.append(f"  {relative}: {allowed}")

        if stale:
            self.fail(
                "ALLOWED_LINES excuses lines that no longer exist. Remove these "
                "entries:\n" + "\n".join(stale)
            )
