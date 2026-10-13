"""Who takes which non-core subject in a class.

``GET`` and ``PUT /v1/academics/classes/<id>/electives/?term=<id>``. The rules
live in :mod:`..services.electives`; this module resolves the class and the
term, answers who may set the choices, and writes the audit event.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event
from vs_config.clock import tenant_today
from vs_rbac.evaluator import has_permission

from ..constants import PERM_CLASSES_ASSIGN, PERM_ELECTIVES_UPDATE, PERM_ELECTIVES_VIEW
from ..exceptions import NotYourClass
from ..models import SchoolClass
from ..serializers import ElectiveClassTermSerializer, ElectiveWriteSerializer
from ..services import electives
from ..services.scoping import scope_to_visible_branches
from .base import AcademicsViewMixin


def may_set_electives(user, tenant, school_class) -> bool:
    """Whether *user* may change who takes what in *school_class*.

    The class's own class teacher, while they are on the staff roll, or a holder
    of ``academics.classes.assign`` over the class's branch (the administrator
    roles). Holding ``academics.electives.update`` is the view's gate and is
    necessary but never sufficient: a teacher holds it for every class they
    teach, and may set only the one they lead.
    """
    teacher = school_class.class_teacher
    if teacher is not None and teacher.user_id == user.pk and teacher.is_on_roll:
        return True
    return has_permission(user, PERM_CLASSES_ASSIGN, tenant=tenant, branch=school_class.branch)


class ClassElectivesView(AcademicsViewMixin, APIView):
    """GET, PUT /v1/academics/classes/<id>/electives/?term=<term id>

    GET answers the non-core subjects of the class's level with how many of the
    roll take each, and the roll with the subjects each student takes. PUT takes
    ``{"changes": [{"student": id, "subject": id, "takes": true|false}]}`` and
    applies all of it or none of it. With no ``term`` the class's year's
    current term is used.

    docstring-name: Who takes what
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_ELECTIVES_UPDATE if self.request.method == "PUT" else PERM_ELECTIVES_VIEW
        )
        return super().get_permissions()

    def _context(self) -> dict:
        """The serializer context: the request Field Access judges the caller by."""
        return {"request": self.request, "multi_branch": self.multi_branch}

    def _class(self, pk) -> SchoolClass:
        """The class, or a 404 when it is another school's or in a branch the caller cannot see."""
        school_class = (
            scope_to_visible_branches(
                SchoolClass.objects.filter(tenant=self.tenant, pk=pk),
                self.request.user, self.tenant,
            )
            .select_related("session", "level", "branch", "class_teacher__user")
            .first()
        )
        if school_class is None:
            raise NotFound("No such class at this school.")
        return school_class

    def _payload(self, school_class, term, today) -> dict:
        data = electives.class_term_payload(self.tenant, school_class, term, today)
        data["can_edit"] = may_set_electives(self.request.user, self.tenant, school_class)
        return ElectiveClassTermSerializer(data, context=self._context()).data

    def get(self, request, pk):
        school_class = self._class(pk)
        today = tenant_today(self.tenant)
        term = electives.resolve_term(
            self.tenant, school_class, request.query_params.get("term"), today,
        )
        return success_response(
            "Who takes what retrieved.", data=self._payload(school_class, term, today),
        )

    def put(self, request, pk):
        school_class = self._class(pk)
        if not may_set_electives(request.user, self.tenant, school_class):
            raise NotYourClass()

        today = tenant_today(self.tenant)
        term = electives.resolve_term(
            self.tenant, school_class, request.query_params.get("term"), today,
        )
        writer = ElectiveWriteSerializer(data=request.data, context=self._context())
        writer.is_valid(raise_exception=True)

        done = electives.apply_changes(
            self.tenant, school_class, term, writer.validated_data["changes"],
            actor=request.user, today=today,
        )
        added, removed = done["added"], done["removed"]
        if added or removed:
            emit_audit_event(
                module_key=AuditModuleKey.ACADEMICS,
                action_type=AuditActionType.UPDATE,
                entity_type="SchoolClass", entity_id=str(school_class.pk),
                entity_label=school_class.name,
                tenant=self.tenant, actor_user=request.user, branch=school_class.branch,
                summary=(
                    f"Who takes what in {school_class.name} for {term.name} changed: "
                    f"{len(added)} added, {len(removed)} removed."
                ),
                metadata={
                    "term": term.pk,
                    "added": [{"student": s, "subject": j} for s, j in added],
                    "removed": [{"student": s, "subject": j} for s, j in removed],
                },
            )
        return success_response(
            f"Who takes what in {school_class.name} saved.",
            data=self._payload(school_class, term, today),
        )
