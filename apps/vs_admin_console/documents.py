"""The requirements-document library: a read-only registry over ``docs/frd/``.

Why a filesystem registry and not a model
-----------------------------------------
The MRD and the per-module FRDs are *generated artefacts* - ``docs/frd/tools/
generate_requirements_documents.py`` writes them, and they are committed to the
repo and reviewed in a PR like any other source. Git is already their version
store, their audit trail and their review gate.

Copying them into the database would buy nothing and cost three things: an
import step that can silently drift from git, ~62 MB of blobs in Postgres (and
``MEDIA_DB_MAX_BYTES`` is a 25 MB per-file ceiling on the DB-backed storage), and
a second source of truth for "which version is current". So the registry reads
the deployed working tree instead: the repo is checked out whole on the server,
so ``docs/`` sits next to ``apps/`` at runtime. A new document is a commit, and
it appears in the console on the next deploy.

The trade-off, stated so nobody discovers it the hard way: **nothing here is
uploadable at runtime.** Adding a document requires a commit. If product ever
needs to publish a PDF without a PR, that is a different feature (a model, an
upload endpoint, and the DB storage path) and not an extension of this one.

Naming is the contract
----------------------
The registry derives everything from filenames, which the generator already
writes to a fixed shape::

    XVS_M23_Purchase_Orders_Delivery_and_AP_Functional_Requirements_Document_v1.3.docx
        └NN┘└──────────── title ───────────┘                                  └ver┘

    XVS_Module_Requirements_Document_v2.15.docx
    XVS_Module_FR_Breakdown_v2.5.docx            (the same lineage, older name)

That is why there is no hand-maintained list of documents in this file: a list
would be a thing to forget to update. See ``docs/frd/README.md``, which is the
prose statement of the same convention.

Path safety
-----------
No caller-supplied string is ever joined into a filesystem path. A download
request names a *document slug* and a *version label*; both are looked up in the
scanned registry, and the path served is the one the scan itself produced. A
traversal attempt cannot express itself, because there is no path parameter to
traverse with.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings


# ``settings.BASE_DIR`` is the ``apps/`` package root, so the repo root - and the
# ``docs/`` tree committed beside it - is one level up.
def documents_root() -> Path:
    """The ``docs/frd`` directory of the deployed working tree."""
    override = getattr(settings, "REQUIREMENTS_DOCS_ROOT", None)
    if override:
        return Path(override)
    return Path(settings.BASE_DIR).parent / "docs" / "frd"


#: Current FRD filenames: ``XVS_M<NN>_<Title>_Functional_Requirements_Document_v<ver>.docx``
_FRD_RE = re.compile(
    r"^XVS_M(?P<number>\d{2})_(?P<title>.+?)_Functional_Requirements_Document_v(?P<version>[\d.]+)\.docx$"
)

#: The one pre-convention FRD still in the tree:
#: ``01_School_and_Branch_Management_FRD_v1.0.docx``. It is a genuine earlier
#: revision of the M01 document, so it is folded into that document's history
#: rather than dropped - a version that exists but is invisible is worse than an
#: oddly named one.
_FRD_LEGACY_RE = re.compile(
    r"^(?P<number>\d{2})_(?P<title>.+?)_FRD_v(?P<version>[\d.]+)\.docx$"
)

#: MRD filenames, current and historical. Per ``docs/frd/README.md`` the
#: ``FR_Breakdown`` files are the same document under its former name, so both
#: patterns feed one lineage.
_MRD_RE = re.compile(
    r"^XVS_Module_(?:Requirements_Document|FR_Breakdown)_v(?P<version>[\d.]+)\.docx$"
)

#: Slug for the single cross-module tracker. Not derived from a module number,
#: because it does not belong to one module.
MRD_SLUG = "module-requirements"


def _version_key(version: str) -> tuple[int, ...]:
    """Sort key for a dotted version label.

    String ordering is wrong here and quietly so: ``"2.9" > "2.15"`` compares
    ``'9'`` against ``'1'`` and puts the older document on top. The tree already
    contains v2.9, v2.9.1, v2.10 and v2.15, so this is a live case, not a
    hypothetical. Comparing integer components fixes it, and zero-padding the
    tuple keeps ``2.6`` and ``2.6.0`` from tying.
    """
    parts = tuple(int(p) for p in version.split(".") if p.isdigit())
    return parts + (0,) * (4 - len(parts))


@dataclass(frozen=True)
class DocumentVersion:
    """One .docx file on disk."""

    version: str
    filename: str
    path: Path
    size_bytes: int

    @property
    def sort_key(self) -> tuple[int, ...]:
        return _version_key(self.version)


@dataclass
class Document:
    """A logical document - one lineage, many versions."""

    slug: str
    title: str
    kind: str  # "MRD" | "FRD"
    module_number: int | None
    versions: list[DocumentVersion] = field(default_factory=list)

    @property
    def current(self) -> DocumentVersion:
        """The highest version. The list is sorted newest-first at build time."""
        return self.versions[0]

    @property
    def sort_key(self) -> tuple[int, int]:
        """Flat ordering: the cross-module MRD first, then FRDs by module number.

        The MRD is not a module and has no number, so it sorts in its own band
        ahead of them rather than being forced to fake a number.
        """
        if self.kind == "MRD":
            return (0, 0)
        return (1, self.module_number or 0)


def _titleise(raw: str) -> str:
    """``Purchase_Orders_Delivery_and_AP`` -> ``Purchase Orders Delivery and AP``.

    Underscores become spaces and nothing else changes: the generator already
    capitalises words, and lowercasing to re-capitalise would destroy the
    acronyms (AP, FRD) that carry the meaning.
    """
    return raw.replace("_", " ").strip()


def _scan(root: Path) -> list[Document]:
    """Walk the docs tree once and build the registry."""
    by_slug: dict[str, Document] = {}

    def add(slug: str, title: str, kind: str, number: int | None, ver: str, path: Path) -> None:
        doc = by_slug.get(slug)
        if doc is None:
            doc = by_slug[slug] = Document(slug=slug, title=title, kind=kind, module_number=number)
        doc.versions.append(
            DocumentVersion(
                version=ver,
                filename=path.name,
                path=path,
                size_bytes=path.stat().st_size,
            )
        )

    mrd_dir = root / "module-requirements"
    if mrd_dir.is_dir():
        for path in mrd_dir.glob("*.docx"):
            match = _MRD_RE.match(path.name)
            if match:
                add(
                    MRD_SLUG,
                    "Module Requirements Document",
                    "MRD",
                    None,
                    match.group("version"),
                    path,
                )

    frd_dir = root / "functional-requirements"
    if frd_dir.is_dir():
        # The folder name (``23-purchase-orders-delivery-and-ap``) is the stable
        # identity: filenames carry the version and so change every revision,
        # while the folder does not.
        for module_dir in sorted(p for p in frd_dir.iterdir() if p.is_dir()):
            slug = module_dir.name
            for path in module_dir.glob("*.docx"):
                match = _FRD_RE.match(path.name) or _FRD_LEGACY_RE.match(path.name)
                if match:
                    add(
                        slug,
                        _titleise(match.group("title")),
                        "FRD",
                        int(match.group("number")),
                        match.group("version"),
                        path,
                    )

    documents = sorted(by_slug.values(), key=lambda d: d.sort_key)
    for doc in documents:
        # Newest first, so ``current`` is versions[0] and the drawer's history
        # reads downward through time.
        doc.versions.sort(key=lambda v: v.sort_key, reverse=True)
    return documents


# The tree changes only when the process is replaced by a deploy, so a scan per
# request would be 42 stat() calls to reach the same answer. Cached behind a lock
# because gunicorn threads can race the first fill.
_cache: list[Document] | None = None
_cache_root: Path | None = None
_lock = threading.Lock()


def get_documents(*, refresh: bool = False) -> list[Document]:
    """The registry, scanned once per process (per docs root)."""
    global _cache, _cache_root
    root = documents_root()
    with _lock:
        if refresh or _cache is None or _cache_root != root:
            _cache = _scan(root)
            _cache_root = root
        return _cache


def clear_cache() -> None:
    """Drop the memoised scan. For tests that write into a temporary docs root."""
    global _cache, _cache_root
    with _lock:
        _cache = None
        _cache_root = None


def find_version(slug: str, version: str | None) -> tuple[Document, DocumentVersion] | None:
    """Resolve a slug (and optional version) to a document and a file on disk.

    Returns ``None`` when either is unknown. ``version=None`` means "the current
    one", which is what a download button without an explicit version wants.

    This is the only way a request reaches a path, and it reaches it by lookup
    rather than by construction - see the module docstring on path safety.
    """
    doc = next((d for d in get_documents() if d.slug == slug), None)
    if doc is None or not doc.versions:
        return None
    if version is None:
        return doc, doc.current
    match = next((v for v in doc.versions if v.version == version), None)
    return (doc, match) if match else None
