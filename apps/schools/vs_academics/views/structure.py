"""Departments, programmes and levels.

The three of them are one screen to a school and one permission resource here,
``academics.structure``. They share the branch rules in
``services/scoping.py``: reads are inclusive, writes follow the caller's
narrowing, and a child may be no wider than its parent.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Prefetch, Q
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    PERM_STRUCTURE_CREATE,
    PERM_STRUCTURE_MANAGE,
    PERM_STRUCTURE_UPDATE,
    PERM_STRUCTURE_VIEW,
)
from ..exceptions import AcademicsError
from ..models import Department, Level, Program, SchoolClass, Subject
from ..serializers import (
    BulkLevelSerializer,
    DepartmentSerializer,
    DepartmentWriteSerializer,
    LevelSerializer,
    LevelWriteSerializer,
    ProgramSerializer,
    ProgramWriteSerializer,
)
from ..services.scoping import (
    UNSET,
    assert_within_parent,
    raised_branch,
    scope_to_visible_branches,
)
from ..services.uniqueness import assert_unique
from ..services.structure import (
    assert_promotion_target,
    generate_code,
    plan_bulk_levels,
)
from .base import AcademicsViewMixin


class ProgramHasLevels(AcademicsError):
    """Deleting a programme that still holds levels.

    PROTECT on Level.program refuses it either way. What this adds is the
    module's own voice: the platform handler pluralises from MODEL names, so
    the reader was told "2 school class and 5 subject offerings still reference
    it" - which names two things a school has never heard of and asks them to
    "reassign" a join row they cannot see.
    """

    error_code = "PROTECTED_REFERENCE"
    http_status = 409


class LevelInUse(AcademicsError):
    """Deleting a level that classes still sit at.

    Only classes block. Subject offerings cascade - see SubjectOffering.level
    for why - so this refusal names one job rather than two, and it is a job the
    school does on the Classes screen.
    """

    error_code = "PROTECTED_REFERENCE"
    http_status = 409


class DepartmentHasPrograms(AcademicsError):
    """Deleting a department that programmes point at.

    Reverses what FRD versions 2.0 to 2.5.1 specified. The foreign key is still
    SET_NULL so no race can destroy a link; this is a service guard in front of
    it, and it names the blocker because the screen renders that verbatim.
    """

    error_code = "PROTECTED_REFERENCE"
    http_status = 409


class _StructureBase(AcademicsViewMixin):
    """Filters and write rules shared by all three."""

    def _filtered(self, qs, *, search_fields=("name", "code")):
        params = self.request.query_params
        qs = scope_to_visible_branches(qs, self.request.user, self.tenant)

        search = (params.get("search") or "").strip()
        if search:
            clause = Q()
            for field in search_fields:
                clause |= Q(**{f"{field}__icontains": search})
            qs = qs.filter(clause)

        is_active = (params.get("is_active") or "").strip().lower()
        if is_active in ("true", "1", "active"):
            qs = qs.filter(is_active=True)
        elif is_active in ("false", "0", "archived", "inactive"):
            qs = qs.filter(is_active=False)
        elif is_active not in ("all",):
            qs = qs.filter(is_active=True)   # active only, unless asked

        branch = (params.get("branch") or "").strip()
        if branch and self.multi_branch:
            if branch.lower() in ("none", "school", "shared"):
                # The one exclusive reading, and it is asked for by name:
                # "show me only what is shared", which is a real question.
                qs = qs.filter(branch__isnull=True)
            else:
                from vs_tenants.references import resolve_branch_reference

                # Inclusive: a null branch means EVERY branch, so a
                # school-wide row belongs to this one too. See _filtered.
                qs = qs.filter(
                    Q(branch__isnull=True)
                    | Q(branch=resolve_branch_reference(self.tenant, branch, "branch")),
                )
        return qs

    def _lens_branch(self):
        """The branch the screen is filtered to, or None for "everything".

        Resolved the same way `_filtered` resolves it, so a count and the rows
        it sits under can never disagree about which branch is in view.
        """
        params = self.request.query_params
        branch = (params.get("branch") or "").strip()
        if not branch or not self.multi_branch:
            return None
        if branch.lower() in ("none", "school", "shared"):
            return None
        from vs_tenants.references import resolve_branch_reference

        return resolve_branch_reference(self.tenant, branch, "branch")

    def _branch_for_write(self, requested=UNSET):
        return raised_branch(self.request.user, self.tenant, requested)

    def _requested_branch(self, validated):
        """UNSET when the caller never mentioned a branch, else the id or None.

        Absent and explicitly null are different answers and must not be
        collapsed: see services/scoping.UNSET.
        """
        if "branch" not in validated:
            return UNSET
        branch = validated["branch"]
        return branch.id if branch is not None else None

    def _code_for(self, model, name, given, *, session=None):
        """A generated code, unique among the ones it has to be unique among.

        `session` for the kinds that belong to a year - a code taken by last
        year's level is free again this year, and suffixing around it would
        produce JSS1-2 in a year that has no JSS1.
        """
        if given:
            return given
        rows = model.all_objects.filter(tenant=self.tenant)
        if session is not None:
            rows = rows.filter(session=session)
        taken = {c.lower() for c in rows.values_list("code", flat=True)}
        return generate_code(name, taken)

    def _unique(self, queryset, **kwargs):
        """Refuse a duplicate before writing it, with a message worth reading.

        The database refuses it either way; what this adds is WHICH field and
        WHAT it collided with. See services/uniqueness.py.
        """
        assert_unique(queryset, multi_branch=self.multi_branch, **kwargs)

    def _audit(self, action, obj, label, summary):
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=action,
            entity_type=type(obj).__name__,
            entity_id=str(obj.pk),
            entity_label=label,
            tenant=self.tenant,
            actor_user=self.request.user,
            summary=summary,
        )


# ── Departments ────────────────────────────────────────────────────────────

def _departments_for(tenant, session=None):
    """Departments, with the two counts the card shows.

    A department has no year of its own on purpose: Sciences is Sciences, and
    giving it a year would make five Sciences rows in five years that only a
    matching NAME could tie back together - which breaks the first time a
    school renames one. What varies by year is what the school RAN under it,
    and that is already in the data.

    So the counts are the year's, not all time. Unfiltered they were adding up
    every year at once: three Sciences subjects taught in three years read as
    "Subjects 9", a number the school has never had. `distinct=True` on both,
    because two counts over two multi-valued joins inflate each other.
    """
    qs = (
        Department.objects.filter(tenant=tenant)
        .select_related("branch")
        # annotate() drops Meta.ordering, and an unordered queryset cannot be
        # paged: the same row lands on two pages, or on none.
        .order_by("name")
    )
    if session is None:
        return qs.annotate(
            program_count_annotated=Count("programs", distinct=True),
            subject_count_annotated=Count("subjects", distinct=True),
        )
    return qs.annotate(
        # A programme is running in a year when it has a level in it. The
        # programme row itself spans every year, so it cannot answer this.
        program_count_annotated=Count(
            "programs", distinct=True,
            filter=Q(programs__levels__session=session),
        ),
        subject_count_annotated=Count(
            "subjects", distinct=True, filter=Q(subjects__session=session),
        ),
    )


class DepartmentListCreateView(_StructureBase, generics.ListCreateAPIView):
    """GET, POST /v1/academics/departments/

    docstring-name: Departments
    """

    serializer_class = DepartmentSerializer

    def get_permissions(self):
        self.rbac_permission = (
            PERM_STRUCTURE_CREATE if self.request.method == "POST"
            else PERM_STRUCTURE_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        return self._filtered(_departments_for(self.tenant, self.session))

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        writer = DepartmentWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        requested = self._requested_branch(data)
        data.pop("branch", None)
        branch = self._branch_for_write(requested)
        data["code"] = self._code_for(Department, data["name"], data.get("code"))
        # all_objects, not objects: an archived department still holds its name
        # and its code, and the constraint does not exempt it either.
        self._unique(
            Department.all_objects.filter(tenant=self.tenant),
            name=data["name"], code=data["code"],
            writing_to_branch=branch is not None,
        )

        dept = Department.objects.create(tenant=self.tenant, branch=branch, **data)
        self._audit(AuditActionType.CREATE, dept, dept.name, f"{dept.name} added.")
        return success_response(
            f"{dept.name} added.",
            data=DepartmentSerializer(
                _departments_for(self.tenant).get(pk=dept.pk),
                context=self.get_serializer_context(),
            ).data,
            status=201,
        )


class DepartmentDetailView(_StructureBase, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/departments/<id>/

    docstring-name: One department
    """

    serializer_class = DepartmentSerializer

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_STRUCTURE_UPDATE, "DELETE": PERM_STRUCTURE_MANAGE,
        }.get(self.request.method, PERM_STRUCTURE_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        return scope_to_visible_branches(
            _departments_for(self.tenant), self.request.user, self.tenant,
        )

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            "Department retrieved.", data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        dept = self.get_object()
        writer = DepartmentWriteSerializer(dept, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        if "branch" in data:
            data["branch"] = self._branch_for_write(self._requested_branch(data))
        self._unique(
            Department.all_objects.filter(tenant=self.tenant),
            name=data.get("name"), code=data.get("code"), exclude_pk=dept.pk,
            writing_to_branch=data.get("branch", dept.branch) is not None,
        )
        for field, value in data.items():
            setattr(dept, field, value)
        dept.save()
        self._audit(AuditActionType.UPDATE, dept, dept.name, f"{dept.name} updated.")
        return success_response(
            f"{dept.name} updated.",
            data=DepartmentSerializer(
                _departments_for(self.tenant).get(pk=dept.pk),
                context=self.get_serializer_context(),
            ).data,
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        dept = self.get_object()
        count = Program.all_objects.filter(department=dept).count()
        if count:
            raise DepartmentHasPrograms(
                f"{count} programme{'s are' if count > 1 else ' is'} mapped to "
                f"{dept.name}. Move {'them' if count > 1 else 'it'} to another "
                f"department first, then delete this one.",
                **{"Program": count},
            )
        name, pk = dept.name, dept.pk
        dept.delete()
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="Department", entity_id=str(pk), entity_label=name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{name} deleted.",
        )
        return success_response(f"{name} deleted.")


# ── Programmes ─────────────────────────────────────────────────────────────

def _programs_for(tenant):
    return Program.objects.filter(tenant=tenant).select_related("branch", "department")


class ProgramListCreateView(_StructureBase, generics.ListCreateAPIView):
    """GET, POST /v1/academics/programs/

    Levels come back nested, because the screen is an accordion and a flat
    list would cost one request per programme to draw one page.

    docstring-name: Programmes and their levels
    """

    serializer_class = ProgramSerializer

    def get_permissions(self):
        self.rbac_permission = (
            PERM_STRUCTURE_CREATE if self.request.method == "POST"
            else PERM_STRUCTURE_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        # The SAME annotations _levels_for carries: one missing here reaches
        # the serializer as its zero default rather than as an error.
        levels = scope_to_visible_branches(
            Level.objects.filter(tenant=self.tenant, session=self.session)
            .select_related("branch", "program", "next_level")
            .annotate(
                class_count_annotated=Count("classes", distinct=True),
                subject_count_annotated=Count("subject_offerings", distinct=True),
            )
            .order_by("order_index"),
            self.request.user, self.tenant,
        )
        return self._filtered(
            _programs_for(self.tenant).prefetch_related(
                Prefetch("levels", queryset=levels, to_attr="visible_levels"),
            ),
        )

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        writer = ProgramWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        requested = self._requested_branch(data)
        data.pop("branch", None)
        branch = self._branch_for_write(requested)
        department = data.get("department")
        if department is not None:
            assert_within_parent(branch, department.branch, parent_label=department.name)
        data["code"] = self._code_for(Program, data["name"], data.get("code"))
        self._unique(
            Program.all_objects.filter(tenant=self.tenant),
            name=data["name"], code=data["code"],
            writing_to_branch=branch is not None,
        )

        program = Program.objects.create(tenant=self.tenant, branch=branch, **data)
        self._audit(
            AuditActionType.CREATE, program, program.name, f"{program.name} added.",
        )
        return success_response(
            f"{program.name} added.",
            data=ProgramSerializer(program, context=self.get_serializer_context()).data,
            status=201,
        )


class ProgramDetailView(_StructureBase, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/programs/<id>/

    docstring-name: One programme
    """

    serializer_class = ProgramSerializer

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_STRUCTURE_UPDATE, "DELETE": PERM_STRUCTURE_MANAGE,
        }.get(self.request.method, PERM_STRUCTURE_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        return scope_to_visible_branches(
            _programs_for(self.tenant), self.request.user, self.tenant,
        )

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            "Programme retrieved.", data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        program = self.get_object()
        writer = ProgramWriteSerializer(program, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        if "branch" in data:
            data["branch"] = self._branch_for_write(self._requested_branch(data))
        target_branch = data.get("branch", program.branch)
        department = data.get("department", program.department)
        if department is not None:
            assert_within_parent(
                target_branch, department.branch, parent_label=department.name,
            )
        self._unique(
            Program.all_objects.filter(tenant=self.tenant),
            name=data.get("name"), code=data.get("code"), exclude_pk=program.pk,
            writing_to_branch=target_branch is not None,
        )
        for field, value in data.items():
            setattr(program, field, value)
        program.save()
        self._audit(
            AuditActionType.UPDATE, program, program.name, f"{program.name} updated.",
        )
        return success_response(
            f"{program.name} updated.",
            data=ProgramSerializer(program, context=self.get_serializer_context()).data,
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        program = self.get_object()
        # EVERY year, not the lens: the rows that block a delete are usually
        # last year's, so the count describing them has to be too.
        held = (
            Level.all_objects.filter(program=program)
            .values_list("session__name", flat=True)
        )
        levels = len(held)
        if levels:
            years = sorted(set(held))
            one_year = len(years) == 1
            where = (
                f"in {years[0]}" if one_year
                else f"across {', '.join(years[:-1])} and {years[-1]}"
            )
            record = (
                "That year is a record" if one_year
                else "Those years are a record"
            )
            raise ProgramHasLevels(
                f"{program.name} still has {levels} "
                f"{'levels' if levels > 1 else 'level'} {where}. {record} of "
                f"what the school ran, so the programme cannot be deleted. To "
                f"stop running it, leave it out of the new year instead.",
                **{"Level": levels},
            )
        name, pk = program.name, program.pk
        program.delete()
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="Program", entity_id=str(pk), entity_label=name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{name} deleted.",
        )
        return success_response(f"{name} deleted.")


# ── Levels ─────────────────────────────────────────────────────────────────

def _levels_for(tenant):
    return (
        Level.objects.filter(tenant=tenant)
        # next_level joined: lazy here is one query per level on the one
        # screen that lists every level.
        .select_related("branch", "program", "next_level")
        .annotate(
            class_count_annotated=Count("classes", distinct=True),
            # So the delete confirmation can say what goes with the level:
            # offerings CASCADE, and silence would remove them unannounced.
            subject_count_annotated=Count("subject_offerings", distinct=True),
        )
        .order_by("program", "order_index")           # annotate() drops Meta.ordering
    )


def _program_or_404(tenant, pk, user):
    program = scope_to_visible_branches(
        Program.objects.filter(tenant=tenant, pk=pk), user, tenant,
    ).first()
    if program is None:
        raise NotFound("No such programme at this school.")
    return program


class LevelListCreateView(_StructureBase, generics.ListCreateAPIView):
    """GET, POST /v1/academics/programs/<id>/levels/

    docstring-name: Levels in a programme
    """

    serializer_class = LevelSerializer

    def get_permissions(self):
        self.rbac_permission = (
            PERM_STRUCTURE_CREATE if self.request.method == "POST"
            else PERM_STRUCTURE_VIEW
        )
        return super().get_permissions()

    @property
    def program(self):
        return _program_or_404(self.tenant, self.kwargs["pk"], self.request.user)

    def get_queryset(self):
        return self._filtered(
            _levels_for(self.tenant).filter(
                program=self.program, session=self.session_required,
            ),
        ).order_by("order_index")

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        program = self.program
        writer = LevelWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        requested = self._requested_branch(data)
        data.pop("branch", None)
        branch = self._branch_for_write(requested)
        assert_within_parent(branch, program.branch, parent_label=program.name)
        data["code"] = self._code_for(
            Level, data["name"], data.get("code"), session=self.session_required,
        )
        # Unique inside a programme AND a year: Year 1 may exist in both
        # Nursery and Primary, and JSS1 comes round again every September.
        self._unique(
            Level.all_objects.filter(program=program, session=self.session_required),
            name=data["name"], code=data["code"], within=program.name,
        )
        if not data.get("order_index"):
            highest = (
                Level.all_objects.filter(program=program, session=self.session_required)
                .order_by("-order_index")
                .values_list("order_index", flat=True).first() or 0
            )
            data["order_index"] = highest + 1

        target = data.pop("next_level", None)
        level = Level.objects.create(
            tenant=self.tenant, program=program, branch=branch,
            session=self.session_required, **data,
        )
        if target is not None:
            assert_promotion_target(level, target)
            level.next_level = target
            level.save(update_fields=["next_level", "updated_at"])

        self._audit(
            AuditActionType.CREATE, level, level.name,
            f"{level.name} added to {program.name}.",
        )
        return success_response(
            f"{level.name} added to {program.name}.",
            data=LevelSerializer(
                _levels_for(self.tenant).get(pk=level.pk),
                context=self.get_serializer_context(),
            ).data,
            status=201,
        )


class LevelBulkCreateView(_StructureBase, APIView):
    """POST /v1/academics/programs/<id>/levels/bulk/

    A duplicate anywhere fails the whole call and creates nothing, naming
    every offender: half-creating a run of levels leaves a school unable to
    tell which of the names it typed took.

    docstring-name: Add levels in bulk
    """

    rbac_permission = PERM_STRUCTURE_CREATE

    @transaction.atomic
    def post(self, request, pk):
        program = _program_or_404(self.tenant, pk, request.user)
        writer = BulkLevelSerializer(data=request.data)
        writer.is_valid(raise_exception=True)

        branch = self._branch_for_write(
            writer.validated_data["branch"] if "branch" in writer.validated_data
            else UNSET,
        )
        assert_within_parent(branch, program.branch, parent_label=program.name)

        taken = {
            c.lower() for c in
            Level.all_objects.filter(tenant=self.tenant, session=self.session_required)
            .values_list("code", flat=True)
        }
        plan = plan_bulk_levels(program, writer.validated_data["names"], taken)
        created = Level.objects.bulk_create([
            Level(
                tenant=self.tenant, program=program, branch=branch,
                session=self.session_required, **row,
            )
            for row in plan
        ])
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.ACADEMIC_STRUCTURE_BULK_CREATED,
            entity_type="Program", entity_id=str(program.pk),
            entity_label=program.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{len(created)} levels added to {program.name}.",
            metadata={"names": [row["name"] for row in plan]},
        )
        word = "level" if len(created) == 1 else "levels"
        return success_response(
            f"{len(created)} {word} added to {program.name}.",
            data=LevelSerializer(
                _levels_for(self.tenant).filter(
                    pk__in=[level.pk for level in created],
                ).order_by("order_index"),
                many=True, context={"multi_branch": self.multi_branch},
            ).data,
            status=201,
        )


class LevelDetailView(_StructureBase, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/levels/<id>/

    docstring-name: One level
    """

    serializer_class = LevelSerializer

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_STRUCTURE_UPDATE, "DELETE": PERM_STRUCTURE_MANAGE,
        }.get(self.request.method, PERM_STRUCTURE_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        return scope_to_visible_branches(
            _levels_for(self.tenant), self.request.user, self.tenant,
        )

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            "Level retrieved.", data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        level = self.get_object()
        writer = LevelWriteSerializer(level, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)

        if "branch" in data:
            data["branch"] = self._branch_for_write(self._requested_branch(data))
            assert_within_parent(
                data["branch"], level.program.branch, parent_label=level.program.name,
            )
        if "next_level" in data:
            cross = str(
                request.query_params.get("cross_program", "")
            ).lower() in ("1", "true", "yes")
            assert_promotion_target(level, data["next_level"], cross_program=cross)

        self._unique(
            Level.all_objects.filter(
                program=level.program, session_id=level.session_id,
            ),
            name=data.get("name"), code=data.get("code"), exclude_pk=level.pk,
            within=level.program.name,
        )
        for field, value in data.items():
            setattr(level, field, value)
        level.save()
        self._audit(AuditActionType.UPDATE, level, level.name, f"{level.name} updated.")
        return success_response(
            f"{level.name} updated.",
            data=LevelSerializer(
                _levels_for(self.tenant).get(pk=level.pk),
                context=self.get_serializer_context(),
            ).data,
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        level = self.get_object()
        classes = SchoolClass.all_objects.filter(level=level).count()
        if classes:
            raise LevelInUse(
                f"{classes} class{'es sit' if classes > 1 else ' sits'} at "
                f"{level.name}. Archive or move "
                f"{'them' if classes > 1 else 'it'} first, then delete the level.",
                **{"SchoolClass": classes},
            )
        # Offerings do not block - they CASCADE. The screen says how many
        # first, so nothing disappears unannounced.
        name, pk = level.name, level.pk
        level.delete()
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="Level", entity_id=str(pk), entity_label=name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{name} deleted.",
        )
        return success_response(f"{name} deleted.")
