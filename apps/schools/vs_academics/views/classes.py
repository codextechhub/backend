"""Classes and subjects.

Two resources, two permission keys - ``academics.classes`` and
``academics.subject`` - and the same branch rules as the rest of the module.

The one lifecycle here is deliberately shallow: a class is archived by clearing
``is_active`` and restored by setting it. That is not the term-and-session
question versions 2.4 and 2.5 of the FRD spent two revisions on, and it does
not reopen it: a class carries no ``archived_at``, nothing derives a current
class from a date, and no invariant says a live class may not have been
archived once.
"""
from __future__ import annotations

import re

from django.db import transaction
from django.db.models import Count, Prefetch, Q
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    PERM_CLASSES_CREATE,
    PERM_CLASSES_MANAGE,
    PERM_CLASSES_UPDATE,
    PERM_CLASSES_VIEW,
    PERM_SUBJECT_CREATE,
    PERM_SUBJECT_MANAGE,
    PERM_SUBJECT_UPDATE,
    PERM_SUBJECT_VIEW,
)
from ..models import Level, SchoolClass, Subject, SubjectOffering
from ..serializers import (
    GenerateArmsSerializer,
    OfferingsWriteSerializer,
    SchoolClassSerializer,
    SchoolClassWriteSerializer,
    SubjectSerializer,
    SubjectWriteSerializer,
)
from ..services.years import assert_year_is_writable
from ..services.scoping import (
    UNSET,
    assert_within_parent,
    scope_to_visible_branches,
)
from ..services.structure import generate_code
from .structure import _StructureBase


def _classes_for(tenant):
    """Classes, with the number of subjects taught at their level."""
    return (
        SchoolClass.objects.filter(tenant=tenant)
        .select_related("branch", "level")
        .annotate(
            subject_count_annotated=Count("level__subject_offerings", distinct=True),
        )
        .order_by("level", "name")                    # annotate() drops Meta.ordering
    )


def _level_or_404(tenant, pk, user):
    """The level a class write names, and the year that level puts it in.

    Only the write paths reach this - creating a class, moving one, generating
    a set of arms - and all three take the class's YEAR from the level rather
    than from the lens, so the lens guard in AcademicsViewMixin.session never
    sees it. Checked here instead, which is the point all three share.
    """
    level = scope_to_visible_branches(
        Level.objects.filter(tenant=tenant, pk=pk), user, tenant,
    ).select_related("branch", "session").first()
    if level is None:
        raise NotFound("No such level at this school.")
    assert_year_is_writable(level.session)
    return level


class _ClassBase(_StructureBase):
    serializer_class = SchoolClassSerializer

    def get_queryset(self):
        qs = self._filtered(_classes_for(self.tenant).filter(session=self.session))
        level = (self.request.query_params.get("level") or "").strip()
        if level:
            qs = qs.filter(level_id=level)
        return qs


class ClassListCreateView(_ClassBase, generics.ListCreateAPIView):
    """GET, POST /v1/academics/classes/

    docstring-name: Classes
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_CLASSES_CREATE if self.request.method == "POST" else PERM_CLASSES_VIEW
        )
        return super().get_permissions()

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        writer = SchoolClassWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)

        level = _level_or_404(self.tenant, data.pop("level").pk, request.user)
        requested = self._requested_branch(data)
        data.pop("branch", None)
        branch = self._branch_for_write(requested)
        assert_within_parent(branch, level.branch, parent_label=level.name)

        data["code"] = self._code_for(
            SchoolClass, data.get("name") or level.name, data.get("code"),
            session=level.session,
        )
        # Two scopes, so two calls: a class NAME is unique inside its level at
        # its branch, while the CODE is unique across the school and year.
        self._unique(
            SchoolClass.all_objects.filter(level=level, branch=branch),
            name=data.get("name"), within=level.name,
        )
        # Per YEAR: JSS1-A is free again every September.
        self._unique(
            SchoolClass.all_objects.filter(
                tenant=self.tenant, session_id=level.session_id,
            ),
            code=data["code"],
        )
        klass = SchoolClass.objects.create(
            tenant=self.tenant, level=level, branch=branch,
            session_id=level.session_id, created_by=request.user, **data,
        )
        self._audit(
            AuditActionType.CREATE, klass, klass.name, f"{klass.name} added.",
        )
        return success_response(
            f"{klass.name} added.",
            data=SchoolClassSerializer(
                _classes_for(self.tenant).get(pk=klass.pk),
                context=self.get_serializer_context(),
            ).data,
            status=201,
        )


class ClassDetailView(_ClassBase, generics.RetrieveUpdateAPIView):
    """GET, PATCH /v1/academics/classes/<id>/

    There is no delete route, and its absence is a promise another module
    depends on rather than a local preference: M11's ClassEnrolment points at
    SchoolClass with on_delete=PROTECT, which is safe precisely because there
    is no route to reach that refusal with. A delete may not be added here
    without M11's agreement.

    docstring-name: One class
    """

    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        self.rbac_permission = (
            PERM_CLASSES_UPDATE if self.request.method == "PATCH" else PERM_CLASSES_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        return scope_to_visible_branches(
            _classes_for(self.tenant), self.request.user, self.tenant,
        )

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            "Class retrieved.", data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        klass = self.get_object()
        writer = SchoolClassWriteSerializer(klass, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)

        level = klass.level
        if "level" in data:
            level = _level_or_404(self.tenant, data.pop("level").pk, request.user)
            klass.level = level
        if "branch" in data:
            data["branch"] = self._branch_for_write(self._requested_branch(data))
        assert_within_parent(
            data.get("branch", klass.branch), level.branch, parent_label=level.name,
        )
        self._unique(
            SchoolClass.all_objects.filter(
                level=level, branch=data.get("branch", klass.branch),
            ),
            name=data.get("name"), exclude_pk=klass.pk, within=level.name,
        )
        self._unique(
            SchoolClass.all_objects.filter(
                tenant=self.tenant, session_id=level.session_id,
            ),
            code=data.get("code"), exclude_pk=klass.pk,
        )
        # Moving a class to a level in another year moves the class with it -
        # the two must agree, and the level is the one that decides.
        klass.session_id = level.session_id
        for field, value in data.items():
            setattr(klass, field, value)
        klass.save()
        self._audit(
            AuditActionType.UPDATE, klass, klass.name, f"{klass.name} updated.",
        )
        return success_response(
            f"{klass.name} updated.",
            data=SchoolClassSerializer(
                _classes_for(self.tenant).get(pk=klass.pk),
                context=self.get_serializer_context(),
            ).data,
        )


class _ClassStateView(_ClassBase, APIView):
    rbac_permission = PERM_CLASSES_MANAGE

    active: bool
    action: str
    verb: str

    def post(self, request, pk):
        klass = scope_to_visible_branches(
            _classes_for(self.tenant), request.user, self.tenant,
        ).filter(pk=pk).first()
        if klass is None:
            raise NotFound("No such class at this school.")

        klass.is_active = self.active
        klass.save(update_fields=["is_active", "updated_at"])
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=self.action,
            entity_type="SchoolClass", entity_id=str(klass.pk),
            entity_label=klass.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{klass.name} {self.verb}.",
        )
        return success_response(
            f"{klass.name} {self.verb}.",
            data=SchoolClassSerializer(
                _classes_for(self.tenant).get(pk=klass.pk),
                context={"multi_branch": self.multi_branch},
            ).data,
        )


class ClassArchiveView(_ClassStateView):
    """POST /v1/academics/classes/<id>/archive/

    docstring-name: Archive a class
    """

    active = False
    action = AuditActionType.ACADEMIC_CLASS_ARCHIVED
    verb = "archived"


class ClassRestoreView(_ClassStateView):
    """POST /v1/academics/classes/<id>/restore/

    docstring-name: Restore a class
    """

    active = True
    action = AuditActionType.ACADEMIC_CLASS_RESTORED
    verb = "restored"


class GenerateArmsView(_ClassBase, APIView):
    """POST /v1/academics/classes/generate-arms/

    Idempotent for the same input: labels already taken in that level and
    branch are skipped rather than refused, so a school that adds a fourth arm
    types A, B, C, D and gets one new class instead of an error about three.

    docstring-name: Generate class arms
    """

    rbac_permission = PERM_CLASSES_CREATE

    @transaction.atomic
    def post(self, request):
        writer = GenerateArmsSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        level = _level_or_404(self.tenant, writer.validated_data["level"], request.user)

        requested = (
            writer.validated_data["branch"]
            if "branch" in writer.validated_data else UNSET
        )
        branch = self._branch_for_write(requested)
        assert_within_parent(branch, level.branch, parent_label=level.name)

        existing = {
            n.lower() for n in
            SchoolClass.all_objects.filter(level=level, branch=branch)
            .values_list("name", flat=True)
        }
        taken = {
            c.lower() for c in
            SchoolClass.all_objects.filter(
                tenant=self.tenant, session_id=level.session_id,
            ).values_list("code", flat=True)
        }

        made = []
        for arm in writer.validated_data["arms"]:
            arm = arm.strip()
            if not arm:
                continue
            name = f"{level.name} {arm}"
            if name.lower() in existing:
                continue                # already there; skipping is the point
            code = _arm_code(level.name, arm, taken)
            taken.add(code.lower())
            existing.add(name.lower())
            made.append(SchoolClass(
                tenant=self.tenant, level=level, branch=branch, name=name,
                arm=arm, code=code, session_id=level.session_id,
                created_by=request.user,
            ))
        created = SchoolClass.objects.bulk_create(made)

        if created:
            emit_audit_event(
                module_key=AuditModuleKey.ACADEMICS,
                action_type=AuditActionType.ACADEMIC_STRUCTURE_BULK_CREATED,
                entity_type="Level", entity_id=str(level.pk),
                entity_label=level.name,
                tenant=self.tenant, actor_user=request.user,
                summary=f"{len(created)} classes created for {level.name}.",
                metadata={"names": [c.name for c in created]},
            )
        word = "class" if len(created) == 1 else "classes"
        return success_response(
            f"{len(created)} {word} created for {level.name}."
            if created else f"Every arm already exists for {level.name}.",
            data=SchoolClassSerializer(
                _classes_for(self.tenant)
                .filter(pk__in=[c.pk for c in created]).order_by("name"),
                many=True, context={"multi_branch": self.multi_branch},
            ).data,
            status=201 if created else 200,
        )


def _arm_code(level_name, arm, taken):
    clean = lambda v: re.sub(r"[^A-Za-z0-9]", "", v or "").upper()
    base = f"{clean(level_name)}-{clean(arm)}"[:20]
    if base.lower() not in taken:
        return base
    for n in range(2, 1000):
        suffix = str(n)
        candidate = f"{base[:20 - len(suffix)]}{suffix}"
        if candidate.lower() not in taken:
            return candidate
    raise ValidationError({"arms": f"Could not build a unique code for {arm}."})


# ── Subjects ───────────────────────────────────────────────────────────────

def _subjects_for(tenant, branch=None):
    """Subjects, with the levels they are offered at.

    ``branch`` narrows the OFFERINGS as well as nothing else - the subject rows
    themselves are filtered by ``_filtered``. Without it the prefetch was called
    ``visible_offerings`` and was not: it counted every level the subject is
    taught at, including levels the branch in view does not have, so under a
    branch lens a card read "Nursery 1-Vocational 2 · 16 levels" at a branch
    that runs fourteen of them. A count shown under a filter has to answer to it.
    """
    offerings = SubjectOffering.objects.select_related("level").order_by(
        "level__program", "level__order_index",
    )
    if branch is not None:
        # A school-wide LEVEL belongs to this branch too - the same inclusive
        # reading the row filter uses, applied one level down.
        offerings = offerings.filter(
            Q(level__branch__isnull=True) | Q(level__branch=branch),
        )
    return (
        Subject.objects.filter(tenant=tenant)
        .select_related("branch", "department")
        .prefetch_related(Prefetch(
            "offerings", queryset=offerings, to_attr="visible_offerings",
        ))
    )


def _resolve_levels(tenant, user, level_ids, *, session):
    """Every id must be this tenant's and this YEAR's, or nothing is written.

    Resolved as a set before anything is written, so one foreign id does not
    leave the valid ids in the same request half-applied.

    The year matters as much as the tenant. An offering says "Mathematics is
    taught at JSS1", and a 2027 subject pointing at the 2026 JSS1 says it
    about a level that stopped existing when that year ended - so the class
    lists and the timetable built on it would read a year that has closed.
    """
    levels = list(
        scope_to_visible_branches(
            Level.objects.filter(tenant=tenant, session=session, pk__in=level_ids),
            user, tenant,
        )
    )
    if len(levels) != len(set(level_ids)):
        raise NotFound("One of those levels does not exist at this school.")
    return levels


class _SubjectBase(_StructureBase):
    serializer_class = SubjectSerializer

    def get_queryset(self):
        qs = self._filtered(
            _subjects_for(self.tenant, self._lens_branch())
            .filter(session=self.session),
        )
        is_core = (self.request.query_params.get("is_core") or "").strip().lower()
        if is_core in ("true", "1", "core"):
            qs = qs.filter(is_core=True)
        elif is_core in ("false", "0", "elective"):
            qs = qs.filter(is_core=False)
        return qs


class SubjectListCreateView(_SubjectBase, generics.ListCreateAPIView):
    """GET, POST /v1/academics/subjects/

    A subject and the levels it is offered at are created in one call, because
    the drawer has one Save button: a two-call create leaves a subject offered
    nowhere whenever the second call fails.

    docstring-name: Subjects
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_SUBJECT_CREATE if self.request.method == "POST" else PERM_SUBJECT_VIEW
        )
        return super().get_permissions()

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        writer = SubjectWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        level_ids = data.pop("level_ids", [])

        requested = self._requested_branch(data)
        data.pop("branch", None)
        branch = self._branch_for_write(requested)
        department = data.get("department")
        if department is not None:
            assert_within_parent(branch, department.branch, parent_label=department.name)
        data["code"] = self._code_for(
            Subject, data["name"], data.get("code"), session=self.session_required,
        )
        self._unique(
            Subject.all_objects.filter(tenant=self.tenant, session=self.session_required),
            name=data["name"], code=data["code"],
            writing_to_branch=branch is not None,
        )

        subject = Subject.objects.create(
            tenant=self.tenant, branch=branch, session=self.session_required, **data,
        )
        if level_ids:
            _write_offerings(
                self.tenant, subject,
                _resolve_levels(
                    self.tenant, request.user, level_ids,
                    session=subject.session,
                ),
            )
        self._audit(
            AuditActionType.CREATE, subject, subject.name, f"{subject.name} added.",
        )
        return success_response(
            f"{subject.name} added.",
            data=SubjectSerializer(
                _subjects_for(self.tenant).get(pk=subject.pk),
                context=self.get_serializer_context(),
            ).data,
            status=201,
        )


class SubjectDetailView(_SubjectBase, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/subjects/<id>/

    docstring-name: One subject
    """

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_SUBJECT_UPDATE, "DELETE": PERM_SUBJECT_MANAGE,
        }.get(self.request.method, PERM_SUBJECT_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        return scope_to_visible_branches(
            _subjects_for(self.tenant), self.request.user, self.tenant,
        )

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            "Subject retrieved.", data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        subject = self.get_object()
        writer = SubjectWriteSerializer(subject, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        level_ids = data.pop("level_ids", None)

        if "branch" in data:
            data["branch"] = self._branch_for_write(self._requested_branch(data))
        target_branch = data.get("branch", subject.branch)
        department = data.get("department", subject.department)
        if department is not None:
            assert_within_parent(
                target_branch, department.branch, parent_label=department.name,
            )
        self._unique(
            Subject.all_objects.filter(
                tenant=self.tenant, session_id=subject.session_id,
            ),
            name=data.get("name"), code=data.get("code"), exclude_pk=subject.pk,
            writing_to_branch=target_branch is not None,
        )
        for field, value in data.items():
            setattr(subject, field, value)
        subject.save()

        if level_ids is not None:
            _write_offerings(
                self.tenant, subject,
                _resolve_levels(
                    self.tenant, request.user, level_ids,
                    session=subject.session,
                ),
            )
        self._audit(
            AuditActionType.UPDATE, subject, subject.name, f"{subject.name} updated.",
        )
        return success_response(
            f"{subject.name} updated.",
            data=SubjectSerializer(
                _subjects_for(self.tenant).get(pk=subject.pk),
                context=self.get_serializer_context(),
            ).data,
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        subject = self.get_object()
        name, pk = subject.name, subject.pk
        subject.delete()                # cascades its offerings
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="Subject", entity_id=str(pk), entity_label=name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{name} deleted.",
        )
        return success_response(f"{name} deleted.")


class SubjectOfferingsView(_SubjectBase, APIView):
    """PUT /v1/academics/subjects/<id>/offerings/

    docstring-name: Where a subject is offered
    """

    rbac_permission = PERM_SUBJECT_UPDATE

    @transaction.atomic
    def put(self, request, pk):
        subject = scope_to_visible_branches(
            _subjects_for(self.tenant), request.user, self.tenant,
        ).filter(pk=pk).first()
        if subject is None:
            raise NotFound("No such subject at this school.")

        writer = OfferingsWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        levels = _resolve_levels(
            self.tenant, request.user, writer.validated_data["level_ids"],
            session=subject.session,
        )
        _write_offerings(self.tenant, subject, levels)

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="Subject", entity_id=str(subject.pk),
            entity_label=subject.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"Where {subject.name} is offered was changed.",
            metadata={"levels": [level.name for level in levels]},
        )
        return success_response(
            f"{subject.name} updated.",
            data=SubjectSerializer(
                _subjects_for(self.tenant).get(pk=subject.pk),
                context={"multi_branch": self.multi_branch},
            ).data,
        )


def _write_offerings(tenant, subject, levels):
    """Replace the set, refusing any level wider than the subject's own scope.

    A shared subject may be offered at any level the school holds. A subject
    bound to one branch may only be offered at levels that are shared or in
    that same branch, which is the containment rule reaching both ends of the
    offering rather than one.
    """
    for level in levels:
        if subject.branch_id is not None and level.branch_id not in (
            None, subject.branch_id,
        ):
            assert_within_parent(
                level.branch, subject.branch, parent_label=subject.name,
            )
    SubjectOffering.all_objects.filter(subject=subject).delete()
    SubjectOffering.objects.bulk_create([
        SubjectOffering(tenant=tenant, subject=subject, level=level)
        for level in levels
    ])
