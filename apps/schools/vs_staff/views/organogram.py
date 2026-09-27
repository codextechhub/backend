"""The school's organogram: units, posts, appointments and dotted lines.

Mounted under ``/v1/i/me/staff/organogram/``.

**Everybody reads the whole chart.** Every member of staff holding
``school.organogram.view`` sees every unit, post and reporting line in the
school, whatever branch they work in: knowing who you report to, and who they
report to, is not a branch matter. Reads are therefore filtered on the tenant
and never narrowed by branch, and a person on the chart carries a name, a
photograph and a job title and nothing else.

**What the chart does not show is behind the register's key.** The dated
history of appointments, the vacancies and the summary say how large the
establishment is, who covered which post when, and how many people are on leave
or suspended. They need ``school.teachers.update``, which the two administrator
roles hold and a teacher does not. ``school.teachers.view`` would have been the
natural key, and it cannot be the gate here, because every teacher holds it:
reading the staff directory is something a teacher does.

**Writing follows the branch.** A unit carries an optional branch, a post is in
its unit's, and an appointment and a dotted line are in their post's. A branch
administrator creates, edits and removes only their own branch's; a
school-wide row is refused with 403, because it speaks for branches they do
not work in. Appointing somebody also needs the caller to manage that person,
by the same rule the staff record uses. Every row carries ``can_manage``.

**Another school's id is a 404**, on read and on write, exactly like an id that
does not exist.

Closed before go-live, like teaching and leave: drawing the chart is not a step
of onboarding, and nothing reads it until the school is running.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from schools.vs_academics.services.scoping import UNSET, assert_may_change, raised_branch

from ..constants import (
    PERM_ORG_ASSIGN,
    PERM_ORG_CREATE,
    PERM_ORG_DELETE,
    PERM_ORG_UPDATE,
    PERM_ORG_VIEW,
    PERM_UPDATE,
)
from ..exceptions import OrganogramInUse
from ..models import (
    StaffMatrixReport,
    StaffOrgNode,
    StaffPosition,
    StaffPositionAssignment,
)
from ..serializers import (
    AppointmentCloseSerializer,
    AppointmentWriteSerializer,
    MatrixReportWriteSerializer,
    OrgNodeWriteSerializer,
    PositionWriteSerializer,
    StaffMatrixReportSerializer,
    StaffOrgNodeSerializer,
    StaffPositionAssignmentSerializer,
    StaffPositionSerializer,
    current_assignment,
    tree_node,
)
from ..services.organogram import StaffOrganogramService, holders_prefetch, holding_q
from ..services.scoping import assert_manages, get_own_profile
from .base import StaffViewMixin

_READ = ("GET", "HEAD", "OPTIONS")

#: Model error keys renamed to the ``*_id`` names a form sends, so a refusal
#: lands on the field the reader has to change.
_FIELD_NAMES = {
    "parent": "parent_id",
    "head_position": "head_position_id",
    "branch": "branch_id",
    "org_node": "org_node_id",
    "reports_to": "reports_to_id",
    "position": "position_id",
    "staff": "staff_id",
}

_TRUE = ("1", "true", "yes")
_FALSE = ("0", "false", "no")
_MAX_ID = 9_223_372_036_854_775_807


def _as_id(raw):
    """A query-parameter id, or None where it cannot name a row.

    ``filter(pk='abc')`` is a 500 from the ORM. Nothing has that id, so the
    honest answer is an empty page rather than a server error.
    """
    value = str(raw).strip()
    if not value.isdigit() or int(value) > _MAX_ID:
        return None
    return int(value)


def validate_model(obj):
    """Run the model's own rules and turn a refusal into a 400 a form can place."""
    try:
        obj.clean()
    except DjangoValidationError as error:
        if hasattr(error, "message_dict"):
            raise ValidationError({
                _FIELD_NAMES.get(field, field): messages
                for field, messages in error.message_dict.items()
            })
        raise ValidationError(error.messages)


def _node_queryset(tenant):
    """Units with their parent, head post, head's holder and child count, prefetched."""
    return (
        StaffOrgNode.objects.filter(tenant=tenant)
        .select_related("branch", "parent", "head_position__org_node")
        .prefetch_related(
            Prefetch(
                "head_position__appointments",
                queryset=(
                    StaffPositionAssignment.all_objects.filter(holding_q())
                    .select_related("staff__user")
                    .order_by("-is_primary", "id")
                ),
                to_attr="current_appointments",
            ),
        )
        .annotate(children_total=Count("children", distinct=True))
        .order_by("name", "id")
    )


def _position_queryset(tenant):
    """Posts with their unit, branch, manager post and holders, prefetched."""
    return (
        StaffPosition.objects.filter(tenant=tenant)
        .select_related("org_node__branch", "reports_to__org_node")
        .prefetch_related(holders_prefetch())
        .order_by("title", "id")
    )


def _appointment_queryset(tenant):
    return (
        StaffPositionAssignment.objects.filter(tenant=tenant)
        .select_related("staff__user", "position__org_node__branch")
        .prefetch_related("staff__additional_postings")
        .order_by("-start_date", "-id")
    )


def _matrix_queryset(tenant):
    return (
        StaffMatrixReport.objects.filter(tenant=tenant)
        .select_related("position__org_node__branch", "reports_to__org_node")
        .order_by("-created_at", "-id")
    )


class _OrganogramView(StaffViewMixin, APIView):
    """What every organogram view shares: the context, the page, the lookups."""

    #: The key each method needs, set per view.
    method_permissions: dict = {}

    @property
    def rbac_permission(self):
        method = (getattr(self.request, "method", "") or "").upper()
        if method in _READ:
            return self.method_permissions.get("GET", PERM_ORG_VIEW)
        return self.method_permissions.get(method, PERM_ORG_UPDATE)

    def context(self) -> dict:
        """One context per request, so per-request answers are asked once."""
        if not hasattr(self, "_context"):
            self._context = {"request": self.request, "tenant": self.tenant}
        return self._context

    def page(self, queryset, serializer_class):
        paginator = XVSPagination()
        rows = paginator.paginate_queryset(queryset, self.request, view=self)
        return paginator.get_paginated_response(
            serializer_class(rows, many=True, context=self.context()).data,
        )

    def _one(self, queryset, pk, noun):
        row = queryset.filter(pk=pk).first()
        if row is None:
            raise NotFound(f"No such {noun} at this school.")
        return row

    def get_node(self, pk):
        return self._one(_node_queryset(self.tenant), pk, "unit")

    def get_position(self, pk):
        return self._one(_position_queryset(self.tenant), pk, "post")

    def get_appointment(self, pk):
        return self._one(_appointment_queryset(self.tenant), pk, "appointment")

    def get_line(self, pk):
        return self._one(_matrix_queryset(self.tenant), pk, "dotted line")

    def may_change(self, obj, noun):
        """403 where the caller may read *obj* and not change it."""
        assert_may_change(
            self.request.user, self.tenant, obj,
            message=(
                f"This {noun} belongs to the whole school or to another branch, "
                f"so only an administrator covering it can change it."
            ),
        )

    def unique_code(self, model, code, *, exclude=None):
        rows = model.all_objects.filter(tenant=self.tenant, code__iexact=code)
        if exclude is not None:
            rows = rows.exclude(pk=exclude)
        if rows.exists():
            raise ValidationError({"code": f"{code} is already used at this school."})


# ── Units ──────────────────────────────────────────────────────────────────


class _UnitWrites(_OrganogramView):
    def unit_branch(self, requested, parent):
        """The branch a unit written by this caller belongs to.

        Left out, a unit under a branch unit takes that branch, because it
        could take no other; otherwise the caller's own branch is filled in
        the way every other school record fills it (``raised_branch``). Naming
        a branch the caller does not cover, or the whole school when they are
        bound to a branch, is refused with 403: it is about who they are, not
        what they typed.
        """
        from vs_rbac.scoping import caller_may_change
        from vs_tenants.references import resolve_branch_reference

        if requested is UNSET and parent is not None and parent.branch_id:
            requested = parent.branch_id
        if requested not in (UNSET, None, ""):
            branch = resolve_branch_reference(self.tenant, requested, "branch_id")
            if not caller_may_change(self.request.user, self.tenant, [branch.pk]):
                raise PermissionDenied(
                    f"You do not work at {branch.name}, so you cannot put a unit there.",
                )
            return branch
        return raised_branch(
            self.request.user, self.tenant,
            None if requested in (None, "") else requested, field="branch_id",
        )

    @staticmethod
    def requested_branch(data):
        """UNSET when the caller never mentioned a branch; None for school-wide."""
        if "branch_id" not in data:
            return UNSET
        return data["branch_id"] or None


class OrgNodeListCreateView(_UnitWrites):
    """GET, POST /v1/i/me/staff/organogram/nodes/

    ``?kind=``, ``?parent=``, ``?roots=true``, ``?is_active=``, ``?search=`` and
    ``?branch=``. The branch filter reads as the chart does: ``?branch=school``
    is the school-wide units alone, and a branch id is that branch's units plus
    the school-wide ones they sit under.

    docstring-name: Organogram units
    """

    method_permissions = {"GET": PERM_ORG_VIEW, "POST": PERM_ORG_CREATE}

    def get(self, request):
        params = request.query_params
        rows = _node_queryset(self.tenant)
        if kind := (params.get("kind") or "").strip():
            rows = rows.filter(kind=kind.upper())
        if parent := params.get("parent"):
            parent_id = _as_id(parent)
            rows = rows.filter(parent_id=parent_id) if parent_id else rows.none()
        if (params.get("roots") or "").lower() in _TRUE:
            rows = rows.filter(parent__isnull=True)
        active = (params.get("is_active") or "").lower()
        if active in _TRUE:
            rows = rows.filter(is_active=True)
        elif active in _FALSE:
            rows = rows.filter(is_active=False)
        if branch := (params.get("branch") or "").strip():
            if branch.lower() == "school":
                rows = rows.filter(branch__isnull=True)
            else:
                branch_id = _as_id(branch)
                rows = (
                    rows.filter(Q(branch__isnull=True) | Q(branch_id=branch_id))
                    if branch_id else rows.none()
                )
        if search := (params.get("search") or "").strip()[:64]:
            rows = rows.filter(Q(name__icontains=search) | Q(code__icontains=search))
        return self.page(rows, StaffOrgNodeSerializer)

    @transaction.atomic
    def post(self, request):
        payload = OrgNodeWriteSerializer(data=request.data, context=self.context())
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        parent = data.get("parent")
        branch = self.unit_branch(self.requested_branch(data), parent)
        code = StaffOrgNode.prefixed_code(data["kind"], data["code"])
        self.unique_code(StaffOrgNode, code)
        node = StaffOrgNode(
            tenant=self.tenant, branch=branch, name=data["name"].strip(), code=code,
            kind=data["kind"], parent=parent, head_position=data.get("head_position"),
            description=data.get("description", ""),
            is_active=data.get("is_active", True),
        )
        validate_model(node)
        node.save()
        return success_response(
            message=f"{node.name} added.",
            data=StaffOrgNodeSerializer(self.get_node(node.pk), context=self.context()).data,
            status=status.HTTP_201_CREATED,
        )


class OrgNodeDetailView(_UnitWrites):
    """GET, PATCH, DELETE /v1/i/me/staff/organogram/nodes/<id>/

    DELETE answers 409 while units or posts still sit under the unit, with both
    counted, rather than deleting them or leaving them without a parent.

    docstring-name: One organogram unit
    """

    method_permissions = {
        "GET": PERM_ORG_VIEW, "PATCH": PERM_ORG_UPDATE, "DELETE": PERM_ORG_DELETE,
    }

    def get(self, request, pk):
        return success_response(
            data=StaffOrgNodeSerializer(self.get_node(pk), context=self.context()).data,
        )

    @transaction.atomic
    def patch(self, request, pk):
        node = self.get_node(pk)
        self.may_change(node, "unit")
        payload = OrgNodeWriteSerializer(
            data=request.data, partial=True, context=self.context(),
        )
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        for field in ("parent", "head_position", "description", "is_active", "kind"):
            if field in data:
                setattr(node, field, data[field])
        if "name" in data:
            node.name = data["name"].strip()
        if "code" in data or "kind" in data:
            node.code = StaffOrgNode.prefixed_code(node.kind, data.get("code", node.code))
            self.unique_code(StaffOrgNode, node.code, exclude=node.pk)
        if "branch_id" in data:
            node.branch = self.unit_branch(self.requested_branch(data), node.parent)
        validate_model(node)
        node.save()
        return success_response(
            message=f"{node.name} updated.",
            data=StaffOrgNodeSerializer(self.get_node(node.pk), context=self.context()).data,
        )

    @transaction.atomic
    def delete(self, request, pk):
        node = self.get_node(pk)
        self.may_change(node, "unit")
        units = StaffOrgNode.all_objects.filter(parent=node).count()
        posts = StaffPosition.all_objects.filter(org_node=node).count()
        if units or posts:
            raise OrganogramInUse(
                f"{node.name} still has "
                + " and ".join(
                    part for part in (
                        f"{units} unit{'s' if units != 1 else ''}" if units else "",
                        f"{posts} post{'s' if posts != 1 else ''}" if posts else "",
                    ) if part
                )
                + " in it. Move or remove them first.",
                units=units, positions=posts,
            )
        name = node.name
        node.delete()
        return success_response(message=f"{name} removed.")


# ── Posts ──────────────────────────────────────────────────────────────────

#: Orderings a caller may ask for. Anything else keeps the title order.
_POSITION_ORDERINGS = {
    "title", "-title", "code", "-code",
    "created_at", "-created_at", "updated_at", "-updated_at",
}


class PositionListCreateView(_OrganogramView):
    """GET, POST /v1/i/me/staff/organogram/positions/

    ``?org_node=``, ``?reports_to=``, ``?is_active=``, ``?search=`` and
    ``?ordering=``. A post is created in a unit, and the unit's branch decides
    whether this caller may create it.

    docstring-name: Organogram posts
    """

    method_permissions = {"GET": PERM_ORG_VIEW, "POST": PERM_ORG_CREATE}

    def get(self, request):
        params = request.query_params
        rows = _position_queryset(self.tenant)
        for name, field in (("org_node", "org_node_id"), ("reports_to", "reports_to_id")):
            if raw := params.get(name):
                value = _as_id(raw)
                rows = rows.filter(**{field: value}) if value else rows.none()
        active = (params.get("is_active") or "").lower()
        if active in _TRUE:
            rows = rows.filter(is_active=True)
        elif active in _FALSE:
            rows = rows.filter(is_active=False)
        if search := (params.get("search") or "").strip()[:64]:
            rows = rows.filter(Q(title__icontains=search) | Q(code__icontains=search))
        if (ordering := params.get("ordering")) in _POSITION_ORDERINGS:
            rows = rows.order_by(ordering, "id")
        return self.page(rows, StaffPositionSerializer)

    @transaction.atomic
    def post(self, request):
        payload = PositionWriteSerializer(data=request.data, context=self.context())
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        self.may_change(data["org_node"], "unit")
        code = data["code"].strip().upper()
        self.unique_code(StaffPosition, code)
        position = StaffPosition(
            tenant=self.tenant, title=data["title"].strip(), code=code,
            org_node=data["org_node"], reports_to=data.get("reports_to"),
            headcount=data.get("headcount", 1), is_active=data.get("is_active", True),
        )
        validate_model(position)
        position.save()
        return success_response(
            message=f"{position.title} added.",
            data=StaffPositionSerializer(
                self.get_position(position.pk), context=self.context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )


class PositionTreeView(_OrganogramView):
    """GET /v1/i/me/staff/organogram/positions/tree/

    The whole chart, nested along the solid lines, or the part under one post
    with ``?root=``. Two queries whatever the size of the school.

    docstring-name: The organogram
    """

    method_permissions = {"GET": PERM_ORG_VIEW}

    def get(self, request):
        root = None
        if raw := request.query_params.get("root"):
            root_id = _as_id(raw)
            root = (
                StaffPosition.objects.filter(tenant=self.tenant, pk=root_id).first()
                if root_id else None
            )
            if root is None:
                raise NotFound("No such post at this school.")
        nodes = StaffOrganogramService.build_tree(self.tenant, root=root)
        context = self.context()
        return success_response(data=[tree_node(node, context) for node in nodes])


class PositionVacanciesView(_OrganogramView):
    """GET /v1/i/me/staff/organogram/positions/vacancies/

    The active posts with an open seat. Behind the register's key rather than
    the chart's: a list of empty posts is a statement about the establishment.

    docstring-name: Vacant posts
    """

    method_permissions = {"GET": PERM_UPDATE}

    def get(self, request):
        rows = StaffOrganogramService.vacancies(self.tenant)
        return success_response(
            data=StaffPositionSerializer(rows, many=True, context=self.context()).data,
        )


class PositionDetailView(_OrganogramView):
    """GET, PATCH, DELETE /v1/i/me/staff/organogram/positions/<id>/

    DELETE answers 409 while anybody has ever been appointed to the post, or
    other posts still report to it. A post somebody held is history, and is
    deactivated instead; a post with reports under it would leave them
    reporting to nobody without anybody having decided that.

    docstring-name: One organogram post
    """

    method_permissions = {
        "GET": PERM_ORG_VIEW, "PATCH": PERM_ORG_UPDATE, "DELETE": PERM_ORG_DELETE,
    }

    def get(self, request, pk):
        return success_response(
            data=StaffPositionSerializer(
                self.get_position(pk), context=self.context(),
            ).data,
        )

    @transaction.atomic
    def patch(self, request, pk):
        position = self.get_position(pk)
        self.may_change(position, "post")
        payload = PositionWriteSerializer(
            data=request.data, partial=True, context=self.context(),
        )
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        if "org_node" in data and data["org_node"].pk != position.org_node_id:
            self.may_change(data["org_node"], "unit")
            position.org_node = data["org_node"]
        for field in ("reports_to", "headcount", "is_active"):
            if field in data:
                setattr(position, field, data[field])
        if "title" in data:
            position.title = data["title"].strip()
        if "code" in data:
            position.code = data["code"].strip().upper()
            self.unique_code(StaffPosition, position.code, exclude=position.pk)
        validate_model(position)
        position.save()
        return success_response(
            message=f"{position.title} updated.",
            data=StaffPositionSerializer(
                self.get_position(position.pk), context=self.context(),
            ).data,
        )

    @transaction.atomic
    def delete(self, request, pk):
        position = self.get_position(pk)
        self.may_change(position, "post")
        appointments = StaffPositionAssignment.all_objects.filter(position=position).count()
        reports = StaffPosition.all_objects.filter(reports_to=position).count()
        if appointments or reports:
            reasons = []
            if appointments:
                reasons.append(
                    f"{appointments} appointment{'s' if appointments != 1 else ''} "
                    f"on record"
                )
            if reports:
                reasons.append(
                    f"{reports} post{'s' if reports != 1 else ''} reporting to it"
                )
            raise OrganogramInUse(
                f"{position.title} has {' and '.join(reasons)}. Deactivate it "
                f"instead, or move its reports first.",
                appointments=appointments, direct_reports=reports,
            )
        title = position.title
        position.delete()
        return success_response(message=f"{title} removed.")


# ── Appointments ───────────────────────────────────────────────────────────


class AppointmentListCreateView(_OrganogramView):
    """GET, POST /v1/i/me/staff/organogram/assignments/

    GET is the dated history, behind the register's key: ``?staff=``,
    ``?position=`` and ``?current=true|false``. POST appoints somebody, which
    needs ``school.organogram.assign``, a post the caller may change, and a
    person the caller manages.

    docstring-name: Appointments
    """

    method_permissions = {"GET": PERM_UPDATE, "POST": PERM_ORG_ASSIGN}

    def get(self, request):
        params = request.query_params
        rows = _appointment_queryset(self.tenant)
        for name, field in (("staff", "staff_id"), ("position", "position_id")):
            if raw := params.get(name):
                value = _as_id(raw)
                rows = rows.filter(**{field: value}) if value else rows.none()
        current = (params.get("current") or "").lower()
        if current in _TRUE:
            rows = rows.filter(end_date__isnull=True)
        elif current in _FALSE:
            rows = rows.filter(end_date__isnull=False)
        return self.page(rows, StaffPositionAssignmentSerializer)

    @transaction.atomic
    def post(self, request):
        payload = AppointmentWriteSerializer(data=request.data, context=self.context())
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        staff = self.get_staff_for_write(data["staff_id"])
        position = data["position"]
        self.may_change(position, "post")
        appointment = StaffOrganogramService.assign_position(
            staff, position, is_primary=data["is_primary"],
            is_acting=data["is_acting"], start_date=data.get("start_date"),
        )
        return success_response(
            message="Appointment made.",
            data=StaffPositionAssignmentSerializer(
                self.get_appointment(appointment.pk), context=self.context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )


class AppointmentDetailView(_OrganogramView):
    """GET /v1/i/me/staff/organogram/assignments/<id>/ - one dated appointment.

    docstring-name: One appointment
    """

    method_permissions = {"GET": PERM_UPDATE}

    def get(self, request, pk):
        return success_response(
            data=StaffPositionAssignmentSerializer(
                self.get_appointment(pk), context=self.context(),
            ).data,
        )


class AppointmentCloseView(_OrganogramView):
    """POST /v1/i/me/staff/organogram/assignments/<id>/close/

    Ends an appointment on ``end_date``, today when it is left out. The row is
    kept; ending an appointment that has already ended changes nothing.

    docstring-name: End an appointment
    """

    method_permissions = {"POST": PERM_ORG_ASSIGN}

    @transaction.atomic
    def post(self, request, pk):
        appointment = self.get_appointment(pk)
        self.may_change(appointment, "appointment")
        assert_manages(request.user, self.tenant, appointment.staff)
        payload = AppointmentCloseSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        StaffOrganogramService.end_assignment(
            appointment, end_date=payload.validated_data.get("end_date"),
        )
        return success_response(
            message="Appointment ended.",
            data=StaffPositionAssignmentSerializer(
                self.get_appointment(pk), context=self.context(),
            ).data,
        )


class CurrentAppointmentsView(_OrganogramView):
    """GET /v1/i/me/staff/organogram/assignments/current/

    Who is in which post today, and whether they are acting: what the chart's
    cards need and nothing more. No dates and no ended appointments.

    docstring-name: Who holds each post
    """

    method_permissions = {"GET": PERM_ORG_VIEW}

    def get(self, request):
        rows = (
            StaffPositionAssignment.objects.filter(tenant=self.tenant)
            .filter(holding_q(), position__is_active=True)
            .select_related("staff__user", "position__org_node")
            .order_by("position__title", "-is_primary", "id")
        )
        context = self.context()
        return success_response(data=[current_assignment(row, context) for row in rows])


class MyAppointmentsView(_OrganogramView):
    """GET /v1/i/me/staff/organogram/assignments/mine/

    The caller's own appointments, dated. Their own history is theirs to read
    whatever the register's key says.

    docstring-name: My appointments
    """

    method_permissions = {"GET": PERM_ORG_VIEW}

    def get(self, request):
        profile = get_own_profile(self.tenant, request.user)
        rows = _appointment_queryset(self.tenant)
        rows = rows.filter(staff=profile) if profile is not None else rows.none()
        return self.page(rows, StaffPositionAssignmentSerializer)


# ── Dotted lines ───────────────────────────────────────────────────────────


class MatrixReportListCreateView(_OrganogramView):
    """GET, POST /v1/i/me/staff/organogram/matrix-reports/

    ``?position=`` and ``?reports_to=``. A dotted line belongs to the post it
    runs from, and that post's branch decides who may draw it.

    docstring-name: Dotted reporting lines
    """

    method_permissions = {"GET": PERM_ORG_VIEW, "POST": PERM_ORG_CREATE}

    def get(self, request):
        params = request.query_params
        rows = _matrix_queryset(self.tenant)
        for name, field in (("position", "position_id"), ("reports_to", "reports_to_id")):
            if raw := params.get(name):
                value = _as_id(raw)
                rows = rows.filter(**{field: value}) if value else rows.none()
        return self.page(rows, StaffMatrixReportSerializer)

    @transaction.atomic
    def post(self, request):
        payload = MatrixReportWriteSerializer(data=request.data, context=self.context())
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        self.may_change(data["position"], "post")
        line = StaffMatrixReport(
            tenant=self.tenant, position=data["position"],
            reports_to=data["reports_to"],
            relationship_label=data.get("relationship_label", "").strip(),
        )
        validate_model(line)
        line.save()
        return success_response(
            message="Dotted line drawn.",
            data=StaffMatrixReportSerializer(
                self.get_line(line.pk), context=self.context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )


class MatrixReportDetailView(_OrganogramView):
    """GET, DELETE /v1/i/me/staff/organogram/matrix-reports/<id>/

    docstring-name: One dotted line
    """

    method_permissions = {"GET": PERM_ORG_VIEW, "DELETE": PERM_ORG_DELETE}

    def get(self, request, pk):
        return success_response(
            data=StaffMatrixReportSerializer(self.get_line(pk), context=self.context()).data,
        )

    @transaction.atomic
    def delete(self, request, pk):
        line = self.get_line(pk)
        self.may_change(line, "dotted line")
        line.delete()
        return success_response(message="Dotted line removed.")


# ── The header ─────────────────────────────────────────────────────────────


class OrganogramSummaryView(_OrganogramView):
    """GET /v1/i/me/staff/organogram/summary/

    Nine numbers for the header above the chart. Behind the register's key,
    because two of them are how many colleagues are on leave or suspended.

    docstring-name: Organogram summary
    """

    method_permissions = {"GET": PERM_UPDATE}

    def get(self, request):
        return success_response(data=StaffOrganogramService.summary(self.tenant))
