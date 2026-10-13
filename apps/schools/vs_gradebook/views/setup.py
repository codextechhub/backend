"""Assessment setup: structures, grading scales and the principal's remark templates.

Reads need ``gradebook.setup.view``. Every write needs ``gradebook.setup.update``
and a caller whose reach is the whole school, because a structure or scale
applies across branches: a branch-bound caller holding the key is refused with a
403 (``SHARED_RECORD_READ_ONLY``) and nothing is written. A record of another
school, or of a branch the caller cannot see, answers 404.

A structure and a scale are served by the same five routes, so one view class
per route takes the kind it manages.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_SETUP_UPDATE, PERM_SETUP_VIEW
from ..serializers import (
    RemarkTemplatesSerializer,
    RemarkTemplatesWriteSerializer,
    ScaleSerializer,
    ScaleWriteSerializer,
    StructureSerializer,
    StructureWriteSerializer,
)
from ..services import setup
from .base import GradebookViewMixin


class _SetupView(GradebookViewMixin, APIView):
    """Reads need the setup view key, writes the update key and a whole-school reach."""

    rbac_permission = PERM_SETUP_VIEW
    write_methods = ("POST", "PATCH", "PUT", "DELETE")

    def get_permissions(self):
        self.rbac_permission = (
            PERM_SETUP_UPDATE if self.request.method in self.write_methods
            else PERM_SETUP_VIEW
        )
        return super().get_permissions()


class _KindView(_SetupView):
    """A setup route for one kind of record: its serializers are the class's own."""

    kind = None
    read_serializer = None
    write_serializer = None

    def render(self, record) -> dict:
        row = setup.record_payload(self.kind, self.tenant, record)
        return self.read_serializer(row, context={"request": self.request}).data

    def record(self, pk):
        return setup.get_record(self.kind, self.tenant, self.branch_scope(), pk)

    def body(self, *, creating=False) -> dict:
        writer = self.write_serializer(
            data=self.request.data,
            context={"request": self.request, "creating": creating},
        )
        writer.is_valid(raise_exception=True)
        return dict(writer.validated_data)


class _ListCreate(_KindView):
    def get(self, request):
        rows = setup.list_records(
            self.kind, self.tenant, self.branch_scope(),
            program=request.query_params.get("program"),
            status=request.query_params.get("status"),
        )
        return success_response(
            data=self.read_serializer(rows, many=True, context={"request": request}).data,
        )

    def post(self, request):
        self.assert_may_configure()
        record = setup.create_record(self.kind, self.tenant, request.user, self.body(creating=True))
        return success_response("Draft created.", self.render(record), status=201)


class _Detail(_KindView):
    def get(self, request, pk):
        return success_response(data=self.render(self.record(pk)))

    def patch(self, request, pk):
        self.assert_may_configure()
        record = self.record(pk)
        record = setup.update_record(self.kind, self.tenant, request.user, record, self.body())
        return success_response("Saved.", self.render(record))

    def delete(self, request, pk):
        self.assert_may_configure()
        setup.delete_record(self.kind, self.tenant, request.user, self.record(pk))
        return success_response("Draft deleted.")


class _Copy(_KindView):
    def post(self, request, pk):
        self.assert_may_configure()
        record = self.record(pk)
        clone = setup.copy_record(self.kind, self.tenant, request.user, record)
        return success_response("Draft created from a copy.", self.render(clone), status=201)


class _Activate(_KindView):
    def post(self, request, pk):
        self.assert_may_configure()
        record = self.record(pk)
        record = setup.activate_record(self.kind, self.tenant, request.user, record)
        return success_response("Put in use.", self.render(record))


class StructureListCreateView(_ListCreate):
    """GET, POST /v1/gradebook/structures/

    ``GET`` lists the school's assessment structures with their components,
    total, verdict, and whether a published result locks them (and which class
    does); ``?program=`` and ``?status=DRAFT|IN_USE|RETIRED`` narrow it. A
    branch-bound caller sees the school-wide ones and their own branches'.
    ``POST`` creates a DRAFT from ``name``, ``program``, optional ``branch``
    (null or absent means every branch) and ``components``, each with ``name``,
    ``short_name``, ``max_score`` and ``is_exam``.

    docstring-name: Assessment structures
    """

    kind = setup.STRUCTURE
    read_serializer = StructureSerializer
    write_serializer = StructureWriteSerializer


class StructureDetailView(_Detail):
    """GET, PATCH, DELETE /v1/gradebook/structures/<id>/

    ``PATCH`` takes any of ``name``, ``branch`` and the whole list of
    ``components`` (a component with an ``id`` is kept, one without is new, one
    left out is removed). A structure a published result rests on, and a retired
    one, cannot be changed; one in use must stay sound and must not orphan a
    mark already entered. ``DELETE`` is for a DRAFT only.

    docstring-name: An assessment structure
    """

    kind = setup.STRUCTURE
    read_serializer = StructureSerializer
    write_serializer = StructureWriteSerializer


class StructureCopyView(_Copy):
    """POST /v1/gradebook/structures/<id>/copy/

    A new DRAFT with the same programme, branch and components, remembering the
    structure it was copied from. Allowed for any structure, locked or retired.

    docstring-name: Copy an assessment structure
    """

    kind = setup.STRUCTURE
    read_serializer = StructureSerializer


class StructureActivateView(_Activate):
    """POST /v1/gradebook/structures/<id>/activate/

    Puts a DRAFT in use for its programme and branch and retires the one in use
    there. Refused unless the components total 100 and at most one is the
    examination (``STRUCTURE_TOTAL``).

    docstring-name: Put an assessment structure in use
    """

    kind = setup.STRUCTURE
    read_serializer = StructureSerializer


class ScaleListCreateView(_ListCreate):
    """GET, POST /v1/gradebook/scales/

    As structures, for grading scales: each with its ``bands`` (``min_score``,
    ``grade``, ``remark``, ``is_fail``) and a ``check`` of ``{ok, message}``
    that says whether the bands leave no gap and no overlap from 0 to 100.

    docstring-name: Grading scales
    """

    kind = setup.SCALE
    read_serializer = ScaleSerializer
    write_serializer = ScaleWriteSerializer


class ScaleDetailView(_Detail):
    """GET, PATCH, DELETE /v1/gradebook/scales/<id>/

    As a structure's detail, for the bands.

    docstring-name: A grading scale
    """

    kind = setup.SCALE
    read_serializer = ScaleSerializer
    write_serializer = ScaleWriteSerializer


class ScaleCopyView(_Copy):
    """POST /v1/gradebook/scales/<id>/copy/

    docstring-name: Copy a grading scale
    """

    kind = setup.SCALE
    read_serializer = ScaleSerializer


class ScaleActivateView(_Activate):
    """POST /v1/gradebook/scales/<id>/activate/

    Refused unless the lowest band starts at 0, every lowest mark is different
    and at least one band is a pass (``SCALE_GAP``, with the reason the editor
    shows).

    docstring-name: Put a grading scale in use
    """

    kind = setup.SCALE
    read_serializer = ScaleSerializer


class RemarkTemplatesView(_SetupView):
    """GET, PUT /v1/gradebook/remark-templates/

    The principal's remark templates, one per band of overall average, highest
    first. The first read seeds the four defaults. ``PUT`` replaces the whole
    list: each ``band_min`` is different and one is 0 (``REMARK_BANDS``).

    docstring-name: Principal's remark templates
    """

    def render(self, data) -> dict:
        return RemarkTemplatesSerializer(data, context={"request": self.request}).data

    def get(self, request):
        return success_response(data=self.render(setup.templates_payload(self.tenant)))

    def put(self, request):
        self.assert_may_configure()
        writer = RemarkTemplatesWriteSerializer(data=request.data, context={"request": request})
        writer.is_valid(raise_exception=True)
        data = setup.replace_templates(
            self.tenant, request.user, [dict(r) for r in writer.validated_data["templates"]],
        )
        return success_response("Remark templates saved.", self.render(data))
