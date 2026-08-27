"""The school year and its terms.

``docstring-name`` lines are what the generated API docs read.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Prefetch, Q
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event
from vs_tenants.references import resolve_branch_reference

from ..constants import (
    PERM_STRUCTURE_CREATE,
    PERM_SESSION_CREATE,
    PERM_SESSION_MANAGE,
    PERM_SESSION_UPDATE,
    PERM_SESSION_VIEW,
)
from ..models import AcademicSession, AcademicTerm, SessionStatus
from ..serializers import SessionSerializer, SessionWriteSerializer, TermSerializer
from ..services.uniqueness import assert_unique
from ..services.sessions import (
    activate_session,
    archive_session,
    assert_term_deletable,
    assert_writable,
    set_branches,
    validate_terms,
)
from .base import AcademicsViewMixin


def _sessions_for(tenant):
    return (
        AcademicSession.objects
        .filter(tenant=tenant)
        .annotate(term_count_annotated=Count("terms", distinct=True))
        .prefetch_related(
            Prefetch("terms", queryset=AcademicTerm.objects.order_by("order_index")),
            "branch_links__branch",
        )
        .order_by("-start_date")                      # annotate() drops Meta.ordering
    )


def _resolve_branches(tenant, branch_ids):
    """Turn the ids a caller sent into branches, refusing foreign ones.

    An empty list is a real answer meaning the whole school, so it is not
    conflated with the field being absent by the caller of this function.

    **Naming a branch twice means naming it once.** ``uq_session_branch``
    refused the repeat with the platform's generic duplicate message, which
    told a caller its request was invalid when it was merely redundant - the
    same mistake the calendar's event audience made. Deduplicated on the
    RESOLVED branch, not on the id, because a caller may name one branch by id
    and the same branch by slug.
    """
    out, seen = [], set()
    for bid in branch_ids:
        branch = resolve_branch_reference(tenant, bid, "branch_ids")
        if branch.pk in seen:
            continue
        seen.add(branch.pk)
        out.append(branch)
    return out


class _SessionBase(AcademicsViewMixin):
    serializer_class = SessionSerializer

    def get_queryset(self):
        qs = _sessions_for(self.tenant)
        status = (self.request.query_params.get("status") or "").strip().upper()
        if status and status != "ALL":
            qs = qs.filter(status=status)
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        branch = (self.request.query_params.get("branch") or "").strip()
        if branch and self.multi_branch:
            # A school-wide session covers every branch, so it matches any
            # branch filter. Anything else would hide the year most schools run.
            resolved = resolve_branch_reference(self.tenant, branch, "branch")
            qs = qs.filter(
                Q(is_school_wide=True) | Q(branch_links__branch=resolved),
            ).distinct()
        return qs


class SessionListCreateView(_SessionBase, generics.ListCreateAPIView):
    """GET, POST /v1/academics/sessions/

    docstring-name: Academic sessions
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_SESSION_CREATE if self.request.method == "POST" else PERM_SESSION_VIEW
        )
        return super().get_permissions()

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        writer = SessionWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        terms = writer.validated_data.pop("terms", [])
        branch_ids = writer.validated_data.pop("branch_ids", [])

        # A year's name is unique per school whatever branches it names, so no
        # scope is passed: "2026/2027 already exists" is the whole rule.
        assert_unique(
            AcademicSession.all_objects.filter(tenant=self.tenant),
            name=writer.validated_data.get("name"),
            multi_branch=self.multi_branch,
        )

        session = AcademicSession.objects.create(
            tenant=self.tenant, **writer.validated_data,
        )
        if terms:
            validate_terms(session, terms)
            AcademicTerm.objects.bulk_create([
                AcademicTerm(tenant=self.tenant, session=session, **t)
                for t in terms
            ])
        set_branches(session, self.tenant, _resolve_branches(self.tenant, branch_ids))

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.CREATE,
            entity_type="AcademicSession",
            entity_id=str(session.pk),
            entity_label=session.name,
            tenant=self.tenant,
            actor_user=request.user,
            summary=f"{session.name} created as a draft session.",
        )
        session = _sessions_for(self.tenant).get(pk=session.pk)
        return success_response(
            f"{session.name} created as a draft session.",
            data=SessionSerializer(session, context=self.get_serializer_context()).data,
            status=201,
        )


class SessionDetailView(_SessionBase, generics.RetrieveUpdateAPIView):
    """GET, PATCH /v1/academics/sessions/<id>/

    docstring-name: One academic session
    """

    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        self.rbac_permission = (
            PERM_SESSION_UPDATE if self.request.method == "PATCH" else PERM_SESSION_VIEW
        )
        return super().get_permissions()

    def retrieve(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_object())
        return success_response("Session retrieved.", data=serializer.data)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        session = self.get_object()
        assert_writable(session)

        writer = SessionWriteSerializer(session, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        terms = writer.validated_data.pop("terms", None)
        branch_ids = writer.validated_data.pop("branch_ids", None)

        if session.status == SessionStatus.ACTIVE and "start_date" in writer.validated_data:
            # A live year's start has already happened. Its name and its end
            # can still be corrected.
            writer.validated_data.pop("start_date")

        assert_unique(
            AcademicSession.all_objects.filter(tenant=self.tenant),
            name=writer.validated_data.get("name"), exclude_pk=session.pk,
            multi_branch=self.multi_branch,
        )

        for field, value in writer.validated_data.items():
            setattr(session, field, value)
        session.save()

        if terms is not None:
            validate_terms(session, terms)
            AcademicTerm.objects.filter(session=session).delete()
            AcademicTerm.objects.bulk_create([
                AcademicTerm(tenant=self.tenant, session=session, **t)
                for t in terms
            ])
        if branch_ids is not None:
            set_branches(
                session, self.tenant, _resolve_branches(self.tenant, branch_ids),
            )

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="AcademicSession",
            entity_id=str(session.pk),
            entity_label=session.name,
            tenant=self.tenant,
            actor_user=request.user,
            summary=f"{session.name} updated.",
        )
        session = _sessions_for(self.tenant).get(pk=session.pk)
        return success_response(
            f"{session.name} updated.",
            data=SessionSerializer(session, context=self.get_serializer_context()).data,
        )


class SessionRollForwardView(AcademicsViewMixin, APIView):
    """POST /v1/academics/sessions/<id>/roll-forward/  {"from": <session id>}

    Seed this year's structure from another year's. See services/rollover.py for
    what is copied and what is deliberately not.

    docstring-name: Copy a year's structure forward
    """

    rbac_permission = PERM_STRUCTURE_CREATE

    def post(self, request, pk):
        from ..models import AcademicSession
        from ..services.rollover import roll_forward

        target = _get_or_404(self.tenant, pk)
        raw = str(request.data.get("from") or "").strip()
        if not raw:
            raise ValidationError({
                "from": "Name the year to copy from.",
            })
        source = AcademicSession.objects.filter(tenant=self.tenant, pk=raw).first()
        if source is None:
            raise NotFound("No such session at this school.")

        written = roll_forward(self.tenant, source=source, target=target)
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.ACADEMIC_STRUCTURE_BULK_CREATED,
            entity_type="AcademicSession", entity_id=str(target.pk),
            entity_label=target.name,
            tenant=self.tenant, actor_user=request.user,
            summary=(
                f"{source.name} structure copied into {target.name}: "
                f"{written['levels']} levels, {written['classes']} classes, "
                f"{written['subjects']} subjects."
            ),
            metadata={"from": source.name, **written},
        )
        return success_response(
            f"{source.name} copied into {target.name}.", data=written,
        )


class SessionActivateView(AcademicsViewMixin, APIView):
    """POST /v1/academics/sessions/<id>/activate/

    docstring-name: Make a session active
    """

    rbac_permission = PERM_SESSION_MANAGE

    def post(self, request, pk):
        session = _get_or_404(self.tenant, pk)
        displaced = activate_session(session, self.tenant, actor=request.user)
        session = _sessions_for(self.tenant).get(pk=session.pk)
        moved = [s.name for s in displaced]
        message = f"{session.name} is now the active session."
        if moved:
            message += " " + _moved_sentence(moved)
        return success_response(
            message,
            data=SessionSerializer(
                session, context={"multi_branch": self.multi_branch},
            ).data,
        )


class SessionArchiveView(AcademicsViewMixin, APIView):
    """POST /v1/academics/sessions/<id>/archive/

    docstring-name: Archive a session
    """

    rbac_permission = PERM_SESSION_MANAGE

    def post(self, request, pk):
        session = _get_or_404(self.tenant, pk)
        archive_session(session, self.tenant, actor=request.user)
        session = _sessions_for(self.tenant).get(pk=session.pk)
        return success_response(
            f"{session.name} archived.",
            data=SessionSerializer(
                session, context={"multi_branch": self.multi_branch},
            ).data,
        )


class TermListCreateView(AcademicsViewMixin, generics.ListCreateAPIView):
    """GET, POST /v1/academics/sessions/<id>/terms/

    docstring-name: Terms in a session
    """

    serializer_class = TermSerializer
    pagination_class = None            # a year has three terms, not three pages

    def get_permissions(self):
        self.rbac_permission = (
            PERM_SESSION_CREATE if self.request.method == "POST" else PERM_SESSION_VIEW
        )
        return super().get_permissions()

    @property
    def session(self):
        return _get_or_404(self.tenant, self.kwargs["pk"])

    def get_queryset(self):
        return AcademicTerm.objects.filter(session=self.session).order_by("order_index")

    def list(self, request, *args, **kwargs):
        data = TermSerializer(self.get_queryset(), many=True).data
        return success_response("Terms retrieved.", data=data)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        session = self.session
        assert_writable(session)

        existing = [
            {
                "name": t.name, "order_index": t.order_index,
                "start_date": t.start_date, "end_date": t.end_date,
            }
            for t in AcademicTerm.objects.filter(session=session)
        ]
        from ..serializers import TermWriteSerializer

        writer = TermWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        # Validated against the siblings it is joining, not on its own: three
        # of the four rules are about a term's relationship to the others.
        validate_terms(session, existing + [dict(writer.validated_data)])

        term = AcademicTerm.objects.create(
            tenant=self.tenant, session=session, **writer.validated_data,
        )
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.CREATE,
            entity_type="AcademicTerm",
            entity_id=str(term.pk),
            entity_label=term.name,
            tenant=self.tenant,
            actor_user=request.user,
            summary=f"{term.name} added to {session.name}.",
        )
        return success_response(
            f"{term.name} added to {session.name}.",
            data=TermSerializer(term).data,
            status=201,
        )


class TermDetailView(AcademicsViewMixin, generics.RetrieveUpdateDestroyAPIView):
    """PATCH, DELETE /v1/academics/terms/<id>/

    There is deliberately no archive route here. A term is never archived on
    its own: it archives with its session, and a term of a draft year is
    corrected by deleting it. A standalone archive would give a row with no
    lifecycle of its own a second one, leading straight back to the state
    activation refuses. A test enumerates this module's URL conf to assert the
    route stays absent, so restoring it in six months fails an existing test.

    docstring-name: One term
    """

    serializer_class = TermSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_permissions(self):
        self.rbac_permission = (
            PERM_SESSION_MANAGE if self.request.method == "DELETE"
            else PERM_SESSION_UPDATE if self.request.method == "PATCH"
            else PERM_SESSION_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        return AcademicTerm.objects.filter(tenant=self.tenant)

    def retrieve(self, request, *args, **kwargs):
        return success_response("Term retrieved.", data=self.get_serializer(self.get_object()).data)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        term = self.get_object()
        assert_writable(term.session)
        from ..serializers import TermWriteSerializer

        writer = TermWriteSerializer(data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        merged = {
            "name": writer.validated_data.get("name", term.name),
            "order_index": writer.validated_data.get("order_index", term.order_index),
            "start_date": writer.validated_data.get("start_date", term.start_date),
            "end_date": writer.validated_data.get("end_date", term.end_date),
        }
        siblings = [
            {
                "name": t.name, "order_index": t.order_index,
                "start_date": t.start_date, "end_date": t.end_date,
            }
            for t in AcademicTerm.objects.filter(session=term.session).exclude(pk=term.pk)
        ]
        validate_terms(term.session, siblings + [merged])

        for field, value in merged.items():
            setattr(term, field, value)
        term.save()
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="AcademicTerm",
            entity_id=str(term.pk),
            entity_label=term.name,
            tenant=self.tenant,
            actor_user=request.user,
            summary=f"{term.name} updated.",
        )
        return success_response(f"{term.name} updated.", data=TermSerializer(term).data)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        term = self.get_object()
        assert_term_deletable(term)
        name, session_name = term.name, term.session.name
        term_id = term.pk
        term.delete()
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="AcademicTerm",
            entity_id=str(term_id),
            entity_label=name,
            tenant=self.tenant,
            actor_user=request.user,
            summary=f"{name} removed from {session_name}.",
        )
        return success_response(f"{name} removed from {session_name}.")


def _get_or_404(tenant, pk):
    session = AcademicSession.objects.filter(tenant=tenant, pk=pk).first()
    if session is None:
        # 404, never 403: another tenant's identifiers must not be enumerable.
        raise NotFound("No such session at this school.")
    return session


def _moved_sentence(names):
    if len(names) == 1:
        return f"{names[0]} no longer covers every branch it did."
    return f"{len(names)} other sessions changed as a result."
