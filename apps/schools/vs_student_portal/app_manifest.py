"""The school's apps as a phone installs them: each school's own name and crest.

The student app and the parent app are each one build, served at ``/student/``
and ``/parent/`` on every school's host. What a phone puts on its home screen
comes from the web app manifest, so the school's host passes each app's
``app/<slug>/manifest.webmanifest`` through to the API, which names the school
and, where it has uploaded a crest, points at icons cut from it, by addresses
relative to the manifest itself. An :class:`AppSpec` says which app: its
address and the line describing it. A school with no crest keeps the app's own
XVS icons, which each app serves at ``<its address>icons/``, so a phone never
shows the bare CodeX mark.

Icons are drawn from the crest on a white square, the crest at 80% of the
square, or at 60% for the maskable icon so a launcher's circle or squircle
never clips it. They are cached per crest, so a new upload is drawn afresh.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from django.core.cache import cache

#: The app's own colours, matching student-fe's static manifest.
THEME_COLOR = "#4A659D"
BACKGROUND_COLOR = "#F9FAFB"

#: Each icon a phone asks for: (pixels, share of the square the crest fills).
ICONS = {
    "icon-192.png": (192, 0.8),
    "icon-512.png": (512, 0.8),
    "maskable-512.png": (512, 0.6),
    "apple-touch-icon.png": (180, 0.8),
}
MANIFEST_ICONS = (
    ("icon-192.png", "192x192", "any"),
    ("icon-512.png", "512x512", "any"),
    ("maskable-512.png", "512x512", "maskable"),
)

#: The most a launcher shows under an icon before it cuts the name off.
SHORT_NAME_LENGTH = 12


@dataclass(frozen=True)
class AppSpec:
    """One of the school's apps: where it is served and how its manifest describes it."""

    path: str
    description: str

    def describe(self, school) -> str:
        return self.description.format(school=school.name)


STUDENT_APP = AppSpec("/student/", "Your classes, results, attendance and fees at {school}.")
PARENT_APP = AppSpec("/parent/", "Your children's results, attendance, notices and fees at {school}.")


def school_for(slug):
    """The school a sign-in-eligible slug names, or None."""
    from vs_tenants.models import Tenant

    tenant = Tenant.objects.filter(
        slug=str(slug or "").strip().lower(),
        status__in=Tenant.AUTHENTICABLE_STATUSES,
    ).first()
    return getattr(tenant, "school_profile", None) if tenant else None


def crest_file(school):
    """The stored crest of *school*, or None when it has uploaded none."""
    from core.models import StoredFile

    branding = getattr(school, "branding", None)
    name = getattr(getattr(branding, "logo", None), "name", "") or ""
    if not name:
        return None
    return StoredFile.objects.filter(name=name, revoked_at__isnull=True).first()


def short_name(name: str) -> str:
    """The name under the home-screen icon: the whole name if it fits, else its first word.

    "Bright Star" stays whole; "Meridian Crest Academy" becomes "Meridian".
    A word is never cut, so "Ogbomosholand College" is "Ogbomosholand" even
    though it runs past the space. student-fe applies the same rule to the
    iPhone's ``apple-mobile-web-app-title``, counting characters as Python
    does, so Android and iPhone show one label.
    """
    whole = " ".join((name or "").split())
    if len(whole) <= SHORT_NAME_LENGTH:
        return whole
    return whole.split(" ", 1)[0]


def manifest(school, *, icon_url, app: AppSpec = STUDENT_APP) -> dict:
    """*app*'s manifest for *school*; ``icon_url(file_name)`` addresses a crest icon."""
    has_crest = crest_file(school) is not None
    return {
        "id": app.path,
        "name": school.name,
        "short_name": short_name(school.name),
        "description": app.describe(school),
        "start_url": app.path,
        "scope": app.path,
        "display": "standalone",
        "orientation": "any",
        "background_color": BACKGROUND_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {
                "src": icon_url(file_name) if has_crest else f"{app.path}icons/{file_name}",
                "sizes": sizes, "type": "image/png", "purpose": purpose,
            }
            for file_name, sizes, purpose in MANIFEST_ICONS
        ],
        # Itself, relative to its own address, so Chrome's
        # getInstalledRelatedApps() can tell the app it is installed.
        "related_applications": [{"platform": "webapp", "url": "manifest.webmanifest"}],
    }


def icon(school, file_name):
    """The PNG bytes of one crest icon, or None (no crest, or not a known icon)."""
    if file_name not in ICONS:
        return None
    stored = crest_file(school)
    if stored is None:
        return None
    key = f"student-app-icon:{stored.pk}:{stored.size}:{file_name}"
    found = cache.get(key)
    if found is None:
        size, share = ICONS[file_name]
        found = draw_icon(bytes(stored.content), size, share)
        if found is None:
            return None
        cache.set(key, found, 60 * 60 * 24)
    return found


def draw_icon(crest: bytes, size: int, share: float):
    """The crest centred on a white square of *size*, filling *share* of it.

    None for bytes that are not an image, so a damaged upload falls back to
    the app's own icons rather than breaking the install.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(crest))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    image = image.convert("RGBA")
    room = max(1, int(size * share))
    image.thumbnail((room, room), Image.LANCZOS)
    if max(image.size) < room:
        scale = room / max(image.size)
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.LANCZOS,
        )
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    canvas.alpha_composite(image, ((size - image.width) // 2, (size - image.height) // 2))
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
