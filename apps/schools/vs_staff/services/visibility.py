"""How much of a colleague's staff profile a reader sees, and why.

Every member of staff reads the whole school's org chart. Opening a person
from it shows more or less of their profile depending on how the reader stands
to that person, and the school decides how much each standing shows. The chart
itself is not narrowed by any of this.

Four audiences
--------------

* ``ADMIN``: the reader's own keys reach the person. The key a tab is read
  under (:data:`GROUP_PERMISSIONS`) is held, and the person is inside the
  reader's branch scope, exactly as :func:`.scoping.scope_staff` draws it. Not
  configurable: each tab is shown as the reader's role allows.
* ``SELF``: the reader is the person.
* ``LINE``: the reader is above the person in the reporting line, at any
  depth. See :func:`_is_above`.
* ``COLLEAGUE``: the reader works at the same school. See
  :func:`_is_colleague`.

The school sets, per configurable audience, which **groups** of the profile it
shows (:data:`GROUPS`). ``contact`` is shown to every audience and cannot be
switched off: a profile with no name on it is not a profile.

Four rules
----------

**Most generous wins.** A reader sees the union of what their keys reach and
what every audience they belong to is granted. A line manager is also a
colleague, and a school that shows colleagues more than line managers has not
thereby hidden it from the line manager.

**The grid grants by relationship, never beyond it.** A teacher who holds no
leave key reads the leave of the people under her because the school ticked
leave for line managers, and still reads nobody else's.

**Field Access stays the ceiling for fields.** A field a reader's role has
switched off under ``school.teachers`` is removed by
:class:`vs_rbac.field_enforcement.FieldAccessMixin` before a group is ever
considered, so the two intersect. The one exception is the person themselves,
who reads their own record through the serializers' owner rule.

**Inside one school, a relationship crosses branches.** A Lekki teacher who
opens an Ikeja colleague from the chart gets the contact card, where the key
path alone would answer 404. Another school's person stays a 404 for everybody.

The policy is ``staff.profile_visibility`` in ``vs_config``, one JSON value per
school, validated here and written through ``set_value`` so every change is
audited. A school that has saved nothing gets :data:`DEFAULT_POLICY`.

Cost
----

Everything a request asks is cached on the request. The relationship costs
three queries at most, whatever the size of the chart: the reader's own staff
record, the open appointments of the reader and the person, and every edge of
the chart (solid and dotted lines together) in one union.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from ..constants import (
    NON_HOLDING_STATUSES,
    PERM_LEAVE_VIEW,
    PERM_ORG_VIEW,
    PERM_RECORDS_VIEW,
    PERM_VIEW,
)

#: The ``vs_config`` definition holding a school's policy.
POLICY_KEY = "staff.profile_visibility"

GROUP_CONTACT = "contact"
GROUP_EMPLOYMENT = "employment"
GROUP_PERSONAL = "personal"
GROUP_RECORDS = "records"
GROUP_LEAVE = "leave"
GROUP_TEACHING = "teaching"
GROUP_HISTORY = "history"
GROUP_ROLES = "roles"

#: The rows of the settings grid, in the order they are drawn.
GROUPS = (
    {
        "key": GROUP_CONTACT,
        "label": "Contact card",
        "description": (
            "Name, photograph, post and unit, branch, sign-in email and phone "
            "number. Shown to everyone who can open the profile."
        ),
        "locked": True,
    },
    {
        "key": GROUP_EMPLOYMENT,
        "label": "Employment",
        "description": (
            "Staff ID, job title, employment type and status, hire and exit "
            "dates, and length of service."
        ),
        "locked": False,
    },
    {
        "key": GROUP_PERSONAL,
        "label": "Personal details",
        "description": "Middle name, date of birth and gender.",
        "locked": False,
    },
    {
        "key": GROUP_RECORDS,
        "label": "Qualifications and documents",
        "description": (
            "The qualifications, certificates and documents the school holds "
            "on them."
        ),
        "locked": False,
    },
    {
        "key": GROUP_LEAVE,
        "label": "Leave",
        "description": "Their leave requests and the days they have taken.",
        "locked": False,
    },
    {
        "key": GROUP_TEACHING,
        "label": "Teaching duties",
        "description": "The classes and subjects they teach.",
        "locked": False,
    },
    {
        "key": GROUP_HISTORY,
        "label": "History",
        "description": "Their employment and account timeline.",
        "locked": False,
    },
    {
        "key": GROUP_ROLES,
        "label": "Roles and access",
        "description": "The roles they hold and the branches those roles reach.",
        "locked": False,
    },
)

GROUP_KEYS = tuple(group["key"] for group in GROUPS)
ALL_GROUPS = frozenset(GROUP_KEYS)

AUDIENCE_ADMIN = "ADMIN"
AUDIENCE_SELF = "SELF"
AUDIENCE_LINE = "LINE"
AUDIENCE_COLLEAGUE = "COLLEAGUE"

#: The columns of the settings grid. ``ADMIN`` is listed so the screen can draw
#: it, and is never part of a stored policy.
AUDIENCES = (
    {
        "key": AUDIENCE_SELF,
        "label": "Themselves",
        "description": "The member of staff reading their own profile.",
        "configurable": True,
    },
    {
        "key": AUDIENCE_LINE,
        "label": "Line managers",
        "description": (
            "Anyone above them in the reporting line, at any level, including "
            "through an acting post or a dotted line."
        ),
        "configurable": True,
    },
    {
        "key": AUDIENCE_COLLEAGUE,
        "label": "Colleagues",
        "description": "Any other member of staff at the school, at any branch.",
        "configurable": True,
    },
    {
        "key": AUDIENCE_ADMIN,
        "label": "Administrators",
        "description": (
            "People whose role reaches staff records at the branch the person "
            "works in. They see what their role allows."
        ),
        "configurable": False,
        "summary": "As their role allows",
    },
)

CONFIGURABLE_AUDIENCES = (AUDIENCE_SELF, AUDIENCE_LINE, AUDIENCE_COLLEAGUE)

#: What a school that has saved nothing shows.
#:
#: A person reads the whole of their own record, teaching duties, history and
#: roles included, since a teacher holds no staff key and it is their own
#: record. A school that wants any of it closed unticks it for ``SELF``.
DEFAULT_POLICY = {
    AUDIENCE_SELF: (
        GROUP_CONTACT, GROUP_EMPLOYMENT, GROUP_PERSONAL, GROUP_RECORDS, GROUP_LEAVE,
        GROUP_TEACHING, GROUP_HISTORY, GROUP_ROLES,
    ),
    AUDIENCE_LINE: (GROUP_CONTACT, GROUP_EMPLOYMENT, GROUP_LEAVE, GROUP_TEACHING),
    AUDIENCE_COLLEAGUE: (GROUP_CONTACT,),
}

#: The same, in the shape a stored value takes.
DEFAULT_POLICY_AS_LISTS = {
    audience: list(groups) for audience, groups in DEFAULT_POLICY.items()
}

#: The key each group's tab is read under: the ``ADMIN`` path.
GROUP_PERMISSIONS = {
    GROUP_CONTACT: PERM_VIEW,
    GROUP_EMPLOYMENT: PERM_VIEW,
    GROUP_PERSONAL: PERM_VIEW,
    GROUP_RECORDS: PERM_RECORDS_VIEW,
    GROUP_LEAVE: PERM_LEAVE_VIEW,
    GROUP_TEACHING: PERM_VIEW,
    GROUP_HISTORY: PERM_VIEW,
    GROUP_ROLES: PERM_VIEW,
}

#: Which group each key of the staff record belongs to.
#:
#: The "on leave today" chip travels with employment rather than leave: it is
#: what the employment status reads as this week, and the directory already
#: shows it beside the status. The leave group is the requests themselves.
FIELD_GROUPS = {
    **dict.fromkeys((
        "full_name", "first_name", "last_name", "email", "phone", "photo_url",
        "organogram", "branch_id", "branch_name", "posting_branch_ids",
        "posted_school_wide", "posting_branches",
    ), GROUP_CONTACT),
    **dict.fromkeys((
        "staff_number", "job_title", "employment_type", "hire_date", "exit_date",
        "employment_status", "employment_status_label", "on_roll",
        "display_employment_status", "display_employment_status_label",
        "on_leave_today", "on_leave_until", "tenure", "lifecycle",
    ), GROUP_EMPLOYMENT),
    **dict.fromkeys(("middle_name", "date_of_birth", "gender"), GROUP_PERSONAL),
    "teaching_load": GROUP_TEACHING,
    "roles": GROUP_ROLES,
    "missing_documents": GROUP_RECORDS,
}

#: Which group each entry of the record's ``counts`` block belongs to.
COUNT_GROUPS = {
    "qualifications": GROUP_RECORDS,
    "documents": GROUP_RECORDS,
    "teaching_assignments": GROUP_TEACHING,
    "leave_requests": GROUP_LEAVE,
}

#: Keys every rendering of the record carries. Anything that is neither here
#: nor in :data:`FIELD_GROUPS` (the account block and its state, the invitation,
#: who created the record) is administration, and is sent to a full view only.
ALWAYS_FIELDS = frozenset({
    "id", "user_id", "can_manage", "_read_only_fields", "profile_view",
    "visible_sections", "counts",
})

PROFILE_FULL = "full"
PROFILE_RESTRICTED = "restricted"

#: How a read was let in. ``SELF`` and ``KEY`` may read a profile as it stood
#: on an earlier day; ``RELATIONSHIP`` may not.
ADMITTED_SELF = "self"
ADMITTED_KEY = "key"
ADMITTED_RELATIONSHIP = "relationship"

_CACHE_ATTR = "_staff_profile_visibility"
_UNSET = object()


# ── The policy ──────────────────────────────────────────────────────────────


def _invalid(detail: dict):
    from vs_config.exceptions import InvalidConfigurationValue

    first = next(iter(detail.values()))[0]
    return InvalidConfigurationValue(first, extra={"key": POLICY_KEY, "detail": detail})


def normalize_policy(value) -> dict:
    """*value* as the policy it describes, or ``InvalidConfigurationValue``.

    Every configurable audience must be present, as a list of group keys drawn
    from :data:`GROUP_KEYS`. ``ADMIN`` is refused by name: it is not the
    school's to set. ``contact`` is added where it was left out, because it is
    on for everybody whatever a client sends. The result lists groups in the
    grid's order, so two saves of the same choice store the same value.

    The refusal's ``extra["detail"]`` names each offending audience, with one
    sentence per problem, for a form to show beside the column.
    """
    if not isinstance(value, dict):
        raise _invalid({"policy": [
            "Send the policy as an object with one list of sections per audience.",
        ]})
    detail: dict[str, list[str]] = {}
    for key in value:
        if key == AUDIENCE_ADMIN:
            detail.setdefault(key, []).append(
                "Administrators see what their role allows. That column cannot "
                "be set.",
            )
        elif key not in CONFIGURABLE_AUDIENCES:
            detail.setdefault(str(key), []).append(
                f"'{key}' is not an audience. Use one of "
                f"{', '.join(CONFIGURABLE_AUDIENCES)}.",
            )
    normalized: dict[str, list[str]] = {}
    for audience in CONFIGURABLE_AUDIENCES:
        groups = value.get(audience)
        if groups is None:
            detail.setdefault(audience, []).append("Say which sections this audience sees.")
            continue
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            detail.setdefault(audience, []).append("Send a list of section names.")
            continue
        unknown = sorted({group for group in groups if group not in ALL_GROUPS})
        for group in unknown:
            detail.setdefault(audience, []).append(
                f"'{group}' is not a section of a staff profile.",
            )
        chosen = {*groups, GROUP_CONTACT}
        normalized[audience] = [group for group in GROUP_KEYS if group in chosen]
    if detail:
        raise _invalid(detail)
    return normalized


def _coerce(stored) -> dict:
    """A stored value read leniently, so a bad row degrades to the default.

    A value can only reach the table through :func:`normalize_policy` (the
    school's endpoint) or through the guard registered on the definition (every
    other write path), so this is a second line rather than the rule.
    """
    stored = stored if isinstance(stored, dict) else {}
    policy = {}
    for audience in CONFIGURABLE_AUDIENCES:
        groups = stored.get(audience)
        if not isinstance(groups, list):
            groups = DEFAULT_POLICY[audience]
        policy[audience] = frozenset(
            {group for group in groups if group in ALL_GROUPS} | {GROUP_CONTACT},
        )
    return policy


def guard_policy(value, *, tenant=None, branch=None) -> None:
    """The ``vs_config`` write guard: refuse any value that is not a policy."""
    normalize_policy(value)


def _definition():
    from vs_config.models import ConfigurationDefinition

    return ConfigurationDefinition.objects.filter(key=POLICY_KEY, is_active=True).first()


def read_policy(tenant, *, request=None) -> dict:
    """``{audience: frozenset(groups)}`` for this school, cached per request."""
    cache = _cache(request)
    key = ("policy", getattr(tenant, "pk", None))
    if key not in cache:
        from vs_config.services.resolution import resolve_value

        definition = _definition()
        if definition is None:
            cache[key] = _coerce(DEFAULT_POLICY_AS_LISTS)
        else:
            value, _row = resolve_value(definition, tenant=tenant)
            cache[key] = _coerce(DEFAULT_POLICY_AS_LISTS if value is None else value)
    return cache[key]



def policy_payload(tenant) -> dict:
    """What the settings screen reads: the policy, where it came from, and the words.

    ``source`` is ``school`` when this school has saved a policy and
    ``default`` when it has not. ``default_policy`` is sent beside it so a
    screen can offer "Back to the default" by saving it.
    """
    from vs_config.services.resolution import resolve_value

    definition = _definition()
    value, row = (None, None) if definition is None else resolve_value(definition, tenant=tenant)
    policy = _coerce(DEFAULT_POLICY_AS_LISTS if value is None else value)
    return {
        "policy": {
            audience: [group for group in GROUP_KEYS if group in policy[audience]]
            for audience in CONFIGURABLE_AUDIENCES
        },
        "source": "school" if row is not None else "default",
        "default_policy": {
            audience: list(groups) for audience, groups in DEFAULT_POLICY_AS_LISTS.items()
        },
        "groups": [dict(group) for group in GROUPS],
        "audiences": [dict(audience) for audience in AUDIENCES],
    }


def write_policy(tenant, actor, value, *, reason: str = "") -> dict:
    """Validate and save a school's policy, audited through ``set_value``."""
    from vs_config.exceptions import ConfigurationError
    from vs_config.services.resolution import set_value

    policy = normalize_policy(value)
    definition = _definition()
    if definition is None:
        raise ConfigurationError(
            "Staff profile visibility is not available on this platform yet.",
            extra={"key": POLICY_KEY},
        )
    set_value(
        definition=definition, value=policy, actor=actor, tenant=tenant,
        reason=reason or "Staff profile visibility set from the school's settings.",
    )
    return policy_payload(tenant)


# ── Who stands where ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProfileAccess:
    """How one reader stands to one person, and what that lets them read.

    ``held`` is the groups whose key the reader holds anywhere; ``in_scope`` is
    whether the person sits inside the reader's branch scope. The two together
    are the ``ADMIN`` path. ``granted`` is what the school's policy gives the
    relationships in ``relationships``.
    """

    is_self: bool
    relationships: frozenset
    held: frozenset
    in_scope: bool
    granted: frozenset

    @property
    def keyed(self) -> frozenset:
        return self.held if self.in_scope else frozenset()

    @property
    def groups(self) -> frozenset:
        return self.keyed | self.granted

    @property
    def is_admin(self) -> bool:
        return GROUP_CONTACT in self.keyed

    @property
    def audience(self):
        """The most generous audience the reader belongs to, or None."""
        if self.is_admin:
            return AUDIENCE_ADMIN
        for audience in CONFIGURABLE_AUDIENCES:
            if audience in self.relationships:
                return audience
        return None

    @property
    def profile_view(self) -> str:
        return PROFILE_FULL if self.is_admin or self.is_self else PROFILE_RESTRICTED

    @property
    def visible_sections(self) -> list:
        groups = self.groups
        return [group for group in GROUP_KEYS if group in groups]

    def admission(self, group):
        """How a read of *group* is let in, or None when it is not.

        The person themselves first, so reading your own record never depends
        on the plan gate the key path asks; then the key path; then the grid.
        """
        if self.is_self and group in self.granted:
            return ADMITTED_SELF
        if group in self.keyed:
            return ADMITTED_KEY
        if group in self.granted:
            return ADMITTED_RELATIONSHIP
        return None


def _cache(request) -> dict:
    """A dict living as long as *request*, or a throwaway one without a request."""
    if request is None:
        return {}
    cache = getattr(request, _CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(request, _CACHE_ATTR, cache)
    return cache


def _viewer_profile(viewer, tenant, cache):
    """``(pk, employment_status)`` of the reader's staff record here, or None."""
    key = ("viewer", viewer.pk, tenant.pk)
    if key not in cache:
        from ..models import StaffProfile

        cache[key] = (
            StaffProfile.all_objects.filter(tenant=tenant, user_id=viewer.pk)
            .values_list("pk", "employment_status")
            .first()
        )
    return cache[key]


def _is_colleague(viewer, profile, tenant) -> bool:
    """Whether the reader works at this school.

    A staff record that is not invited, resigned or terminated, which is the
    same line :func:`.organogram.holding_q` draws for a seat. A reader with no
    staff record at all (a school's first administrator often has none) counts
    where they hold the key to read the chart.
    """
    if profile is not None and profile[1] not in NON_HOLDING_STATUSES:
        return True
    from vs_rbac.evaluator import has_permission

    return has_permission(viewer, PERM_ORG_VIEW, tenant=tenant)


def _chart_edges(tenant, cache) -> dict:
    """``{post: {posts it reports to}}``, solid and dotted lines, in one query."""
    key = ("edges", tenant.pk)
    if key not in cache:
        from ..models import StaffMatrixReport, StaffPosition

        solid = StaffPosition.all_objects.filter(
            tenant=tenant, reports_to__isnull=False,
        ).values_list("pk", "reports_to_id")
        dotted = StaffMatrixReport.all_objects.filter(tenant=tenant).values_list(
            "position_id", "reports_to_id",
        )
        edges: dict = {}
        for child, parent in solid.union(dotted, all=True):
            edges.setdefault(child, set()).add(parent)
        cache[key] = edges
    return cache[key]


def _is_above(viewer_staff_pk, subject, tenant, cache) -> bool:
    """Whether the reader sits above *subject* on the chart, at any depth.

    The reader's posts are every seat they hold (:func:`.organogram.holding_q`:
    primary, acting and secondary alike, suspended included, since a suspended
    manager still holds the post). The person's posts are their open primary
    and acting appointments. From those, the walk climbs every solid and every
    dotted line, so a post with a dotted line to a post the reader holds, or
    under one that has, is under the reader. A post shared with the reader is
    not above it: co-holders are peers.
    """
    from ..models import StaffPositionAssignment
    from .organogram import holding_q

    rows = StaffPositionAssignment.all_objects.filter(tenant=tenant).filter(
        (Q(staff_id=viewer_staff_pk) & holding_q())
        | (
            Q(staff_id=subject.pk, end_date__isnull=True)
            & (Q(is_primary=True) | Q(is_acting=True))
        )
    ).values_list("staff_id", "position_id")
    held, starts = set(), set()
    for staff_id, position_id in rows:
        (held if staff_id == viewer_staff_pk else starts).add(position_id)
    if not held or not starts:
        return False

    edges = _chart_edges(tenant, cache)
    seen: set = set()
    frontier = [parent for post in starts for parent in edges.get(post, ())]
    while frontier:
        post = frontier.pop()
        if post in seen:
            continue
        if post in held:
            return True
        seen.add(post)
        frontier.extend(edges.get(post, ()))
    return False


def relationships(viewer, subject, tenant, *, request=None) -> frozenset:
    """Which of ``SELF``, ``LINE`` and ``COLLEAGUE`` the reader stands in to *subject*.

    Empty for another school's person, whatever the reader holds. ``COLLEAGUE``
    covers only a person who works here (not invited, resigned or terminated):
    a leaver's contact card is for the people whose keys reach them.
    """
    if (
        viewer is None
        or not getattr(viewer, "pk", None)
        or tenant is None
        or subject.tenant_id != tenant.pk
    ):
        return frozenset()
    cache = _cache(request)
    key = ("relationships", viewer.pk, subject.pk)
    if key in cache:
        return cache[key]

    found = set()
    profile = _viewer_profile(viewer, tenant, cache)
    is_self = subject.user_id == viewer.pk
    if is_self:
        found.add(AUDIENCE_SELF)
    if (
        subject.employment_status not in NON_HOLDING_STATUSES
        and _is_colleague(viewer, profile, tenant)
    ):
        found.add(AUDIENCE_COLLEAGUE)
    if not is_self and profile is not None and _is_above(profile[0], subject, tenant, cache):
        found.add(AUDIENCE_LINE)
    cache[key] = frozenset(found)
    return cache[key]


def _held_groups(viewer, tenant) -> frozenset:
    """The groups whose key the reader holds, wherever the person sits."""
    from vs_rbac.evaluator import get_effective_permissions
    from vs_rbac.permissions import is_vision_super_admin

    if is_vision_super_admin(viewer):
        return ALL_GROUPS
    keys = get_effective_permissions(viewer, tenant=tenant)
    return frozenset(group for group, key in GROUP_PERMISSIONS.items() if key in keys)


def in_scope(viewer, subject, tenant, *, visible=_UNSET) -> bool:
    """Whether *subject* is inside the reader's branch scope.

    The same answer :func:`.scoping.scope_staff` gives, read off the row in hand
    rather than asked of the database: the whole school, the school-wide
    people, the reader's own branches, and the reader themselves.
    """
    from vs_rbac.permissions import is_vision_super_admin
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    if subject.user_id == getattr(viewer, "pk", None) or is_vision_super_admin(viewer):
        return True
    if visible is _UNSET:
        visible = visible_branch_ids(viewer, tenant)
    if visible is WHOLE_TENANT or subject.branch_id is None:
        return True
    return subject.branch_id in visible


def profile_access(request, subject, tenant, *, visible=_UNSET) -> ProfileAccess:
    """How the request's caller stands to *subject*, cached per request."""
    viewer = getattr(request, "user", None)
    cache = _cache(request)
    key = ("access", getattr(viewer, "pk", None), subject.pk)
    if key in cache:
        return cache[key]
    if (
        viewer is None
        or not getattr(viewer, "is_authenticated", False)
        or tenant is None
        or subject.tenant_id != tenant.pk
    ):
        access = ProfileAccess(False, frozenset(), frozenset(), False, frozenset())
    else:
        found = relationships(viewer, subject, tenant, request=request)
        policy = read_policy(tenant, request=request)
        granted = frozenset().union(*(policy[audience] for audience in found))
        access = ProfileAccess(
            is_self=AUDIENCE_SELF in found,
            relationships=found,
            held=_held_groups(viewer, tenant),
            in_scope=in_scope(viewer, subject, tenant, visible=visible),
            granted=granted,
        )
    cache[key] = access
    return access


def allowed_groups(request, subject, tenant) -> frozenset:
    """Every group of *subject*'s profile the request's caller may read."""
    return profile_access(request, subject, tenant).groups


def audience_for(request, subject, tenant):
    """The most generous audience the request's caller belongs to, or None."""
    return profile_access(request, subject, tenant).audience


def shape_record(data: dict, access) -> dict:
    """A rendered staff record cut down to what *access* may read.

    Keys outside the reader's groups are removed, not masked, and the
    administration keys (the account block, the invitation, who created the
    record) go to a full view only. ``counts`` keeps the entries whose group is
    readable and is removed when none is. ``profile_view`` and
    ``visible_sections`` are added for the screen to choose its tabs by.

    ``access=None`` is a render acting for nobody, which keeps everything.
    """
    if access is None:
        data["profile_view"] = PROFILE_FULL
        data["visible_sections"] = list(GROUP_KEYS)
        return data
    groups = access.groups
    full = access.profile_view == PROFILE_FULL
    for name in list(data):
        if name in ALWAYS_FIELDS:
            continue
        group = FIELD_GROUPS.get(name)
        if (group is None and not full) or (group is not None and group not in groups):
            data.pop(name)
    counts = data.get("counts")
    if isinstance(counts, dict):
        kept = {
            name: value for name, value in counts.items()
            if COUNT_GROUPS.get(name) in groups
        }
        if kept:
            data["counts"] = kept
        else:
            data.pop("counts")
    if not full and "can_manage" in data:
        data["can_manage"] = False
    data["profile_view"] = access.profile_view
    data["visible_sections"] = access.visible_sections
    return data
