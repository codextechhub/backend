"""REST views for vs_workflow. See urls.py for the full routing table."""

from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, mixins
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from vs_tenants.models import Tenant
from vs_rbac.permissions import user_has_rbac_permission

from vs_workflow.exceptions import TemplateInvalidError
from vs_workflow.constants import (
    PERM_TEMPLATE_MANAGE, PERM_TEMPLATE_VIEW,
    PERM_INSTANCE_SUBMIT, PERM_INSTANCE_VIEW, PERM_INSTANCE_CANCEL,
    PERM_ACTION_REVERSE, PERM_GROUP_MANAGE, PERM_GROUP_VIEW,
    ApproverSource, GroupMemberKind, OrganogramTarget,
)
from vs_workflow.models import (
    ApprovalDelegation, WorkflowApproverGroup, WorkflowApproverGroupMember,
    WorkflowInstance, WorkflowStage, WorkflowStageAction, WorkflowStageApproverOverride,
    WorkflowStageApprover, WorkflowStageInstance, WorkflowTemplate,
)
from vs_workflow.serializers import (
    ApprovalDelegationSerializer, ApproverPreviewRequestSerializer,
    CancelInstanceSerializer, ReverseActionSerializer,
    StageActionWriteSerializer, SubmitForApprovalSerializer,
    WorkflowApproverGroupMemberWriteSerializer, WorkflowApproverGroupSerializer,
    WorkflowStageApproverOverrideSerializer,
    WorkflowInstanceDetailSerializer, WorkflowInstanceListSerializer,
    WorkflowTemplatePublishSerializer, WorkflowTemplateReadSerializer,
)
from vs_workflow.services import actions as actions_svc
from vs_workflow.services import comparison as comparison_svc
from vs_workflow.services import my_queue as my_queue_svc
from vs_workflow.services import release as release_svc
from vs_workflow.services import submission as submission_svc
from vs_workflow.services import templates as templates_svc
from vs_workflow.services.approvers import (
    describe_group_members, resolve_approvers, resolve_group_users,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

# Apply branch scope only when the user is branch-scoped.
def _filter_by_branch(qs, branch):
    """Narrow to what a branch-scoped user may see: their own branch's rows
    plus the tenant-wide ones.

    Branch-pinned rows are an override of the tenant-wide default, not a
    replacement for it (see WorkflowTemplate.branch), so an exact-match filter
    left branch users with an empty list whenever the tenant published at
    tenant level - which is the normal case.
    """
    if branch is not None:
        return qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    return qs


# Dry-run a DYNAMIC_ROLE stage's rules against a sample document.
def _preview_dynamic_role(d, requester, instance, scope):
    """Return (eligible approvers, rule trace) for unsaved dynamic rules.

    Mirrors what the engine will do at activation - validate each condition,
    take the first match, resolve that role's assignees - so the builder's
    answer and the engine's answer come from the same rules. Raises
    TemplateInvalidError on a malformed rule, which is the point: the builder
    should learn about a bad operator here, not from a stuck approval.
    """
    from vs_rbac.models import TenantRoleTemplate
    from vs_workflow.conditions import evaluate_condition, validate_condition
    from vs_workflow.services.approvers import EligibleApprover, _users_for_roles

    document = d.get("sample_document") or {}
    evaluations, matched = [], None

    for i, raw in enumerate(d["dynamic_role_rules"]):
        where = f"Rule {i + 1}"
        key = (raw or {}).get("role_key") or ""
        if not key:
            raise TemplateInvalidError(f"{where}: 'role_key' is required.")
        role = TenantRoleTemplate.objects.filter(
            tenant=requester.tenant, key=key,
            status=TenantRoleTemplate.Status.ACTIVE).first()
        if role is None:
            raise TemplateInvalidError(
                f"{where}: no active role with key '{key}' exists in this tenant.")
        condition = raw.get("condition")
        validate_condition(condition, where)
        hit, trace = evaluate_condition(condition, document)
        evaluations.append({
            "order": i, "role_key": key, "role_name": role.name,
            "is_fallback": condition in (None, {}),
            "trace": trace, "picked": False,
        })
        if hit:
            matched = role
            evaluations[-1]["picked"] = True
            break

    if matched is None:
        return [], {"matched_role_key": None, "matched_role_name": None,
                    "evaluations": evaluations,
                    "note": "No rule matched and there is no fallback rule, so "
                            "this stage would resolve to nobody."}

    branch = instance.branch if scope == "BRANCH" else None
    users = _users_for_roles([matched.pk], requester.tenant, branch)
    users = [u for u in users if u.pk != requester.pk]
    return ([EligibleApprover(user=u) for u in users],
            {"matched_role_key": matched.key, "matched_role_name": matched.name,
             "evaluations": evaluations})


# Resolve tenant/branch context once for all workflow views.
class TenantScopedMixin:
    """Single source of truth for "which tenant is this request about".

    ``request.tenant`` is resolved by TenantJWTAuthentication from the asserted
    ``?tenant=`` and is always present on an authenticated request. This
    replaces an earlier ``get_school()`` that read ``request._cached_school`` -
    an attribute nothing in the codebase ever set, so every scope check built
    on it silently passed. Models whose default manager is tenant-aware were
    still scoped by the ambient context; models without one (stage instances,
    stage actions) were not scoped at all.
    """

    def get_tenant(self):
        return getattr(self.request, "tenant", None)

    def get_branch(self):
        return getattr(self.request.user, "branch", None)


# ── Templates ────────────────────────────────────────────────────────────────

class WorkflowTemplateViewSet(
    TenantScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet,
):
    """docstring-name: Workflow templates"""
    serializer_class = WorkflowTemplateReadSerializer

    def get_permissions(self):
        # Publishing templates requires manage rights; read endpoints use view rights.
        self.rbac_permission = (
            PERM_TEMPLATE_MANAGE
            if self.action in ("publish", "use_platform_version", "adoption", "compare")
            else PERM_TEMPLATE_VIEW
        )
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_serializer(self, *args, **kwargs):
        """Attach the platform/tenant counterpart map before anything serializes.

        The pairing is a property of the page as a whole, so it is resolved in
        one query here rather than per row inside the serializer, which would be
        one query per template.
        """
        if args:
            objs = args[0]
            objs = list(objs) if isinstance(objs, (list, tuple)) else [objs]
            kwargs.setdefault("context", self.get_serializer_context())
            kwargs["context"] = kwargs["context"] | {
                "counterparts": self._counterparts(objs),
            }
        return super().get_serializer(*args, **kwargs)

    def _counterparts(self, objs):
        """Map (document_type, code) -> {"platform": row|None, "mine": row|None}."""
        keys = {(o.document_type, o.code) for o in objs if isinstance(o, WorkflowTemplate)}
        if not keys:
            return {}
        match = Q()
        for document_type, code in keys:
            match |= Q(document_type=document_type, code=code)
        rows = (WorkflowTemplate.all_objects
                .filter(match)
                .filter(Q(tenant=self.get_tenant()) | Q(tenant__isnull=True))
                .only("id", "tenant", "branch", "document_type", "code",
                      "updated_at", "is_active"))
        out = {}
        for row in rows:
            pair = out.setdefault((row.document_type, row.code),
                                  {"platform": None, "mine": None})
            # An inactive tenant version is not "their own" any more: they asked
            # for the platform's back, so the screen must not claim otherwise.
            if row.tenant_id is None:
                # The shared definition belongs to no branch; a branch-scoped
                # row with no tenant would be a data error, not a platform one.
                if row.branch_id is None:
                    pair["platform"] = row
            elif row.is_active:
                pair["mine"] = row
        return out

    def get_queryset(self):
        # Explicitly scoped rather than relying on the tenant-aware manager's
        # ambient context. Global (tenant-less) templates stay visible, which is
        # what include_global on the manager means.
        qs = WorkflowTemplate.all_objects.filter(
            Q(tenant=self.get_tenant()) | Q(tenant__isnull=True))
        # Branch-wide is *narrowing*, not exclusive: the cascade a branch user
        # runs under is branch → tenant → platform, so all three have to be
        # listable. Filtering to branch=<theirs> hid the tenant-wide and shared
        # templates that actually decide most of their documents.
        branch = self.get_branch()
        if branch is not None:
            qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
        # Explicit ordering: the model has none, and paginating an unordered
        # queryset returns rows in an undefined order across pages.
        return qs.prefetch_related("stages", "routes").order_by("document_type", "code")

    @action(detail=False, methods=["post"], url_path="preview-approvers")
    def preview_approvers(self, request):
        """Resolve the eligible approvers for an ad-hoc stage config + sample
        requester WITHOUT persisting anything. Powers the template builder's
        live "who would approve?" preview. Honours both approver sources and
        active delegations, exactly like activation-time resolution."""
        s = ApproverPreviewRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        from django.contrib.auth import get_user_model
        UserModel = get_user_model()
        requester = UserModel.objects.filter(pk=d["requester"]).first()
        if requester is None:
            return Response({"detail": "Requester not found."}, status=status.HTTP_404_NOT_FOUND)

        # Build a transient (unsaved) stage from the posted config.
        stage = WorkflowStage(
            approver_source=d["approver_source"],
            organogram_target=d.get("organogram_target", "") or "",
            organogram_levels=d.get("organogram_levels", 1) or 1,
            approver_role_key=d.get("approver_role_key", "") or "",
            approver_scope=d.get("approver_scope"),
        )
        if d["approver_source"] == ApproverSource.ORGANOGRAM and \
                d.get("organogram_target") == OrganogramTarget.SPECIFIC_POSITION:
            try:
                from vs_user.models import Position
                stage.organogram_position = Position.objects.filter(code=d["organogram_position_code"]).first()
            except ImportError:
                stage.organogram_position = None
        if d["approver_source"] == ApproverSource.ROLE:
            from vs_rbac.models import TenantRoleTemplate
            exists = TenantRoleTemplate.objects.filter(
                tenant=requester.tenant, key=d["approver_role_key"],
                status=TenantRoleTemplate.Status.ACTIVE,
            ).exists()
            if not exists:
                # A mistyped role key deserves loud feedback in the builder,
                # not a silent empty approver list.
                return Response(
                    {"detail": f"No active role with key '{d['approver_role_key']}' "
                               "exists in this tenant."},
                    status=status.HTTP_404_NOT_FOUND)
        if d["approver_source"] == ApproverSource.WORKFLOW_GROUP:
            group = WorkflowApproverGroup.all_objects.filter(
                tenant=requester.tenant, code=d["approver_group_code"], is_active=True,
            ).first()
            if group is None:
                return Response(
                    {"detail": f"No active approver group with code "
                               f"'{d['approver_group_code']}' exists in this tenant."},
                    status=status.HTTP_404_NOT_FOUND)
            stage.approver_group = group

        # Build a transient instance carrying just the context the resolver reads.
        instance = WorkflowInstance(
            requested_by=requester,
            tenant=requester.tenant,
            branch=getattr(requester, "branch", None),
            document_type=d.get("document_type", "") or "",
        )

        rule_preview = None
        if d["approver_source"] == ApproverSource.DYNAMIC_ROLE:
            # Dynamic rules live on stage.dynamic_rules, a reverse FK that an
            # unsaved stage cannot carry, so the preview evaluates the posted
            # rules directly instead of persisting a throwaway stage.
            try:
                eligible, rule_preview = _preview_dynamic_role(
                    d, requester, instance, stage.approver_scope)
            except TemplateInvalidError as exc:
                return Response({"detail": exc.message},
                                status=status.HTTP_400_BAD_REQUEST)
        else:
            eligible = resolve_approvers(stage, instance)

        def _u(user):
            if user is None:
                return None
            return {
                "id": str(user.pk),
                "full_name": getattr(user, "full_name", "") or user.get_username(),
                "email": getattr(user, "email", ""),
            }

        approvers = [{"user": _u(e.user), "on_behalf_of": _u(e.on_behalf_of)} for e in eligible]
        payload = {
            "approver_source": d["approver_source"],
            "organogram_target": d.get("organogram_target") or None,
            "count": len(approvers),
            "approvers": approvers,
        }
        if rule_preview is not None:
            payload["dynamic_role"] = rule_preview
        return Response(payload, status=status.HTTP_200_OK)

    def _platform_oversight(self, template):
        """Refuse anything but a platform actor reading a shared template.

        These two endpoints are the only place the console reads across tenant
        boundaries, so the gate is explicit and in one place rather than implied
        by the queryset: the caller's own tenant must be the platform one, and
        the subject must be the shared template. Returns an error Response, or
        None when the caller may proceed.
        """
        if getattr(self.request.tenant, "kind", None) != Tenant.Kind.PLATFORM:
            return Response({
                "success": False,
                "message": "Only the platform can see how tenants have adjusted a template.",
                "error": {"code": "PLATFORM_ONLY", "detail": {}},
            }, status=status.HTTP_403_FORBIDDEN)
        if template.tenant_id is not None:
            return Response({
                "success": False,
                "message": "Only a shared template has tenant versions to compare.",
                "error": {"code": "NOT_PLATFORM_TEMPLATE", "detail": {}},
            }, status=status.HTTP_400_BAD_REQUEST)
        return None

    @action(detail=True, methods=["get"])
    def adoption(self, request, pk=None):
        """Who runs this shared template as published, and who runs their own.

        Editing a shared template reaches only the tenants still following it.
        This is that number, so the person editing knows whether they are
        changing the path for forty tenants or for four.
        """
        template = self.get_object()
        refusal = self._platform_oversight(template)
        if refusal is not None:
            return refusal
        return Response({
            "template": {
                "id": template.pk, "name": template.name,
                "document_type": template.document_type, "code": template.code,
                "updated_at": template.updated_at,
            },
            **comparison_svc.adoption_for(template),
        })

    @action(detail=True, methods=["get"])
    def compare(self, request, pk=None):
        """How one tenant's version of this template differs from the shared one.

        `?with=<template id>`. The other template must be an active tenant
        version of this same (document_type, code) - the pairing is checked
        server-side, so this cannot be used to read an arbitrary tenant
        template by guessing an id. Configuration only: no documents, no
        approvals, no people.
        """
        template = self.get_object()
        refusal = self._platform_oversight(template)
        if refusal is not None:
            return refusal

        other_id = request.query_params.get("with")
        if not other_id:
            return Response({"detail": "Pass ?with=<template id>."},
                            status=status.HTTP_400_BAD_REQUEST)
        other = (WorkflowTemplate.all_objects
                 .filter(pk=other_id, document_type=template.document_type,
                         code=template.code, tenant__isnull=False)
                 .select_related("tenant").first())
        if other is None:
            # Same answer for "no such template" and "not a version of this
            # one", so the endpoint cannot be used to probe which ids exist.
            raise NotFound("No tenant version of this template with that id.")

        return Response({
            "base": {"id": template.pk, "name": template.name,
                     "updated_at": template.updated_at},
            "other": {"id": other.pk, "name": other.name,
                      "tenant_slug": other.tenant.slug,
                      "tenant_name": other.tenant.name,
                      "updated_at": other.updated_at},
            **comparison_svc.compare_templates(template, other),
        })

    @action(detail=False, methods=["post"], url_path="publish")
    def publish(self, request):
        p = WorkflowTemplatePublishSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        d = p.validated_data

        # Publishing the shared definition is a platform act. Codex is itself a
        # tenant, so without this every "master" it published would have been
        # its own private template that no other tenant inherits.
        as_platform = d.get("scope") == "PLATFORM"
        if as_platform and getattr(request.tenant, "kind", None) != Tenant.Kind.PLATFORM:
            return Response({
                "success": False,
                "message": "Only the platform can publish a shared template.",
                "error": {"code": "PLATFORM_SCOPE_DENIED", "detail": {}},
            }, status=status.HTTP_403_FORBIDDEN)

        # Template publishing replaces stage/route configuration through the service layer.
        t = templates_svc.publish_template(
            tenant=None if as_platform else request.tenant,
            # A shared template belongs to no branch; carrying the publisher's
            # own branch would scope it out of every tenant that inherits it.
            branch=None if as_platform else self.get_branch(),
            document_type=d["document_type"], code=d["code"], name=d["name"],
            description=d.get("description", ""),
            notification_events=d.get("notification_events", {}),
            created_by=request.user,
            stages_payload=d["stages"], routes_payload=d.get("routes", []),
        )
        return Response(self.get_serializer(t).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="use-platform-version")
    def use_platform_version(self, request, pk=None):
        """Stop running this tenant's own version and follow the platform's again.

        The tenant's version is switched off rather than deleted: an instance
        PROTECTs the template it ran under, so the version that has actually
        been used is precisely the one that cannot be removed. Switched off, it
        drops out of the submission cascade and the next request falls through
        to the platform template. Publishing again brings it back.
        """
        template = self.get_object()
        if template.tenant_id is None:
            return Response({
                "success": False,
                "message": "This is the platform's own template, so there is nothing to fall back to.",
                "error": {"code": "ALREADY_PLATFORM", "detail": {}},
            }, status=status.HTTP_400_BAD_REQUEST)

        platform = (WorkflowTemplate.all_objects
                    .filter(tenant__isnull=True, branch__isnull=True, is_active=True,
                            document_type=template.document_type, code=template.code)
                    .first())
        if platform is None:
            # Refusing is the honest answer: switching this off would leave the
            # document type with no template at all, and every submission of it
            # would fail at the point of submitting.
            return Response({
                "success": False,
                "message": "There is no platform version of this template to fall back to. "
                           "Adjust this one instead.",
                "error": {"code": "NO_PLATFORM_VERSION", "detail": {}},
            }, status=status.HTTP_409_CONFLICT)

        template.is_active = False
        template.save(update_fields=["is_active", "updated_at"])
        return Response(self.get_serializer(platform).data)


# ── Instances ────────────────────────────────────────────────────────────────

class WorkflowInstanceViewSet(
    TenantScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet,
):
    """docstring-name: Workflow instances"""
    def get_permissions(self):
        if self.action == "create":
            self.rbac_permission = PERM_INSTANCE_SUBMIT
        elif self.action == "cancel":
            self.rbac_permission = PERM_INSTANCE_CANCEL
        elif self.action in ("list", "retrieve"):
            self.rbac_permission = PERM_INSTANCE_VIEW
        else:
            # Actor-level actions are guarded by ownership/eligibility in the service layer.
            return [IsAuthenticatedAndActive()]
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_serializer_class(self):
        return WorkflowInstanceDetailSerializer if self.action == "retrieve" else WorkflowInstanceListSerializer

    def get_queryset(self):
        # Instance lists are tenant-scoped before any user-supplied filters apply.
        qs = (WorkflowInstance.all_objects.filter(tenant=self.get_tenant())
              .select_related("template", "current_stage")
              .prefetch_related("stage_instances__stage", "stage_instances__actions",
                                "stage_instances__eligible_approvers", "audit_logs")
              .order_by("-updated_at", "-created_at"))
        p = self.request.query_params
        if p.get("document_type"): qs = qs.filter(document_type=p["document_type"])
        if p.get("status"):        qs = qs.filter(status=p["status"])
        if p.get("requested_by"):  qs = qs.filter(requested_by_id=p["requested_by"])
        if p.get("template_code"): qs = qs.filter(template__code=p["template_code"])
        return qs

    def create(self, request):
        p = SubmitForApprovalSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        d = p.validated_data
        try:
            ct = ContentType.objects.get(pk=d["content_type_id"])
            document = ct.model_class().objects.get(pk=d["object_id"])
        except Exception:
            return Response({
                "success": False,
                "message": "The referenced document was not found.",
                "error": {"code": "DOCUMENT_NOT_FOUND", "detail": {}},
            }, status=status.HTTP_404_NOT_FOUND)
        # Submission service validates the document handler, template, and initial routing.
        instance = submission_svc.submit_for_approval(
            document=document, requested_by=request.user,
            template_code=d.get("template_code") or None,
        )
        return Response(
            WorkflowInstanceDetailSerializer(instance).data
            | {"approval": release_svc.approval_block(instance)},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        instance = actions_svc.withdraw(self.get_object().id, request.user)
        return Response(WorkflowInstanceDetailSerializer(instance).data)

    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):
        instance = actions_svc.resubmit(self.get_object().id, request.user)
        return Response(WorkflowInstanceDetailSerializer(instance).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        p = CancelInstanceSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        instance = actions_svc.cancel(
            self.get_object().id, request.user, p.validated_data["reason"])
        return Response(WorkflowInstanceDetailSerializer(instance).data)

    @action(detail=True, methods=["post"], url_path="continue-without-approval")
    def continue_without_approval(self, request, pk=None):
        """POST - step past a stage nobody can approve, and record who chose to.

        Offered when a submission parks: the template requires an approval nobody holds
        the permission for, so the document would otherwise wait indefinitely. The
        release is refused if anybody at all can decide the stage, which is what keeps
        this from being a self-approval button on a document that has a reviewer.

        Guarded by ownership rather than a permission key, deliberately: this is the
        submitter's own escape from their own stuck submission. See
        ``services.release.may_release``.

        docstring-name: Continue without approval
        """
        instance = self.get_object()
        if not release_svc.may_release(instance, request.user):
            return Response({
                "success": False,
                "message": "Only the person who submitted this can continue it without approval.",
                "error": {"code": "NOT_THE_SUBMITTER", "detail": {}},
            }, status=status.HTTP_403_FORBIDDEN)
        try:
            release_svc.release_parked_stage(
                instance, actor_user=request.user,
                reason=(request.data or {}).get("reason"),
            )
        except release_svc.NotParkedError as exc:
            # Somebody became able to approve between the warning and the click. The
            # document is fine; it just needs a decision now, so this is not an error
            # state the client should treat as a failure to submit.
            return Response({
                "success": False,
                "message": str(exc),
                "error": {"code": "NOT_PARKED", "detail": release_svc.describe_park(instance)},
            }, status=status.HTTP_409_CONFLICT)
        except ValueError as exc:
            return Response({
                "success": False,
                "message": str(exc),
                "error": {"code": "INVALID_REASON", "detail": {}},
            }, status=status.HTTP_400_BAD_REQUEST)
        instance.refresh_from_db()
        return Response(WorkflowInstanceDetailSerializer(instance).data)

    @action(detail=True, methods=["post"], url_path="actions")
    def record_action(self, request, pk=None):
        p = StageActionWriteSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        instance = actions_svc.record_action(
            self.get_object().id, request.user,
            action=p.validated_data["action"],
            comment=p.validated_data.get("comment", ""),
        )
        return Response(WorkflowInstanceDetailSerializer(instance).data)


class ReverseActionView(TenantScopedMixin, APIView):
    """docstring-name: Reverse an approval action"""
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = PERM_ACTION_REVERSE

    def post(self, request, action_id):
        p = ReverseActionSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        try:
            row = WorkflowStageAction.objects.select_related(
                "stage_instance__instance").get(pk=action_id)
        except WorkflowStageAction.DoesNotExist:
            raise NotFound("Action not found.")
        # WorkflowStageAction has no tenant column and no tenant-aware manager,
        # so this comparison is the only thing standing between a reverse-capable
        # admin and another tenant's approval history. It must never be
        # conditional on a value that can be absent.
        if row.stage_instance.instance.tenant_id != getattr(self.get_tenant(), "pk", None):
            # Hide cross-tenant action existence behind the same 404.
            raise NotFound("Action not found.")
        reversal = actions_svc.reverse_action(action_id, request.user, p.validated_data["reason"])
        return Response({"reversal_action_id": str(reversal.id)})


# ── Dashboards ────────────────────────────────────────────────────────────────

class PendingApprovalsView(TenantScopedMixin, APIView):
    """GET /workflow/dashboard/pending/ - instances where the user is eligible to act.

    docstring-name: My pending approvals
    """
    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        # Which snapshots are actionable lives in services/my_queue so the console
        # landing screen counts this queue by exactly the rules it lists it by.
        snaps = my_queue_svc.pending_approval_snapshots(request.user, self.get_tenant())
        results = []
        for snap in snaps:
            inst = snap.stage_instance.instance
            results.append(WorkflowInstanceListSerializer(inst).data | {
                "awaiting_on_stage": snap.stage_instance.stage.label,
                "awaiting_since": snap.stage_instance.activated_at,
                "on_behalf_of": str(snap.on_behalf_of_id) if snap.on_behalf_of_id else None,
            })
        return Response({"results": results, "count": len(results)})


class MySubmissionsView(TenantScopedMixin, APIView):
    """GET /workflow/dashboard/submitted/ - instances the user has submitted.

    docstring-name: My submissions
    """
    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        # Submitter dashboard is restricted to the caller's own submitted instances.
        qs = (WorkflowInstance.all_objects
              .filter(tenant=self.get_tenant(), requested_by=request.user)
              .select_related("template", "current_stage")
              .order_by("-updated_at", "-created_at"))
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        return Response(WorkflowInstanceListSerializer(qs, many=True).data)


class TeamLoadView(TenantScopedMixin, APIView):
    """GET /workflow/dashboard/team-load/ - active instance counts by stage.

    docstring-name: Team approval load
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = PERM_INSTANCE_VIEW

    def get(self, request):
        # Count active stage instances by document type/stage for operational
        # load. WorkflowStageInstance has no tenant-aware manager, so the join
        # to the instance's tenant is the scope - unconditionally, or the board
        # reports every tenant's workload.
        qs = (WorkflowStageInstance.objects
              .filter(status="ACTIVE", instance__tenant=self.get_tenant())
              .values("instance__document_type", "stage__code", "stage__label")
              .order_by("instance__document_type", "stage__code"))
        buckets = defaultdict(lambda: {"count": 0, "stage_label": None})
        for row in qs:
            key = (row["instance__document_type"], row["stage__code"])
            buckets[key]["count"] += 1
            buckets[key]["stage_label"] = row["stage__label"]
        return Response([
            {"document_type": dt, "stage_code": code,
             "stage_label": info["stage_label"], "active_count": info["count"]}
            for (dt, code), info in sorted(buckets.items())
        ])


# ── Approver groups ───────────────────────────────────────────────────────────

class WorkflowApproverGroupViewSet(TenantScopedMixin, ModelViewSet):
    """Named approver pools behind the Workflow Approver screen.

    docstring-name: Workflow approver groups
    """
    serializer_class = WorkflowApproverGroupSerializer

    _WRITE_ACTIONS = {"create", "update", "partial_update", "destroy",
                      "add_member", "remove_member"}

    def get_permissions(self):
        # Reading the groups travels with template management for the same
        # reason the role list does: a WORKFLOW_GROUP stage names a group, and
        # the builder cannot offer one it is not allowed to read. Writing a
        # group still takes the group's own manage key.
        self.rbac_permission = (
            PERM_GROUP_MANAGE if self.action in self._WRITE_ACTIONS
            else [PERM_GROUP_VIEW, PERM_TEMPLATE_MANAGE]
        )
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_serializer_context(self):
        return super().get_serializer_context() | {"tenant": self.request.tenant}

    def get_queryset(self):
        qs = (WorkflowApproverGroup.all_objects
              .filter(tenant=self.get_tenant())
              .prefetch_related("members__user", "members__role", "members__position"))
        if self.request.query_params.get("is_active") in ("true", "false"):
            qs = qs.filter(is_active=self.request.query_params["is_active"] == "true")
        if self.request.query_params.get("search"):
            term = self.request.query_params["search"]
            qs = qs.filter(Q(name__icontains=term) | Q(code__icontains=term))
        return qs.order_by("name")

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant, created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """Refuse to delete a group a template still points at.

        The FK is PROTECT, so the alternative is a 500. Deactivating keeps the
        stage resolvable (to nobody) and preserves audit history.
        """
        group = self.get_object()
        used_by = list(group.workflow_stages.filter(retired_at__isnull=True)
                       .values_list("template__code", "code")[:10])
        if used_by:
            return Response({
                "success": False,
                "message": "This group is used by one or more workflow stages. "
                           "Deactivate it instead, or repoint those stages first.",
                "error": {
                    "code": "APPROVER_GROUP_IN_USE",
                    "detail": {"stages": [f"{t}:{s}" for t, s in used_by]},
                },
            }, status=status.HTTP_409_CONFLICT)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["get"])
    def resolve(self, request, pk=None):
        """Who this group resolves to right now, per member and in total.

        Powers the screen's "resolves to N people" affordance. Runs the same
        resolution the engine runs at stage activation, so the preview cannot
        disagree with reality. `?branch=<id>` previews branch narrowing for
        ROLE members the way a BRANCH-scoped stage would see them.
        """
        group = self.get_object()
        branch = None
        branch_id = request.query_params.get("branch")
        if branch_id:
            # The shared resolver, not a hand-rolled filter: it was the last
            # site still travelling ``school__tenant``, and it also handed a
            # non-numeric or oversized ``?branch=`` straight to the database,
            # which is a 500 where a 404 belongs.
            from vs_tenants.references import find_branch_in_tenant
            branch = find_branch_in_tenant(request.tenant, branch_id)
            if branch is None:
                return Response({"detail": "Branch not found."},
                                status=status.HTTP_404_NOT_FOUND)

        members = describe_group_members(group, request.tenant, branch)
        people = resolve_group_users(group, request.tenant, branch)
        return Response({
            "group": {"id": str(group.pk), "code": group.code,
                      "name": group.name, "is_active": group.is_active},
            "members": members,
            "resolved_count": len(people),
            "resolved_users": [
                {"id": str(u.pk),
                 "name": getattr(u, "full_name", "") or u.get_username(),
                 "email": u.email}
                for u in people
            ],
        })

    @action(detail=True, methods=["post"], url_path="members")
    def add_member(self, request, pk=None):
        """Add one person, role, or position to the group."""
        group = self.get_object()
        s = WorkflowApproverGroupMemberWriteSerializer(
            data=request.data, context={"tenant": request.tenant})
        s.is_valid(raise_exception=True)
        d = s.validated_data
        target = d["resolved_target"]
        field = {GroupMemberKind.USER: "user", GroupMemberKind.ROLE: "role",
                 GroupMemberKind.POSITION: "position"}[d["kind"]]

        member, created = WorkflowApproverGroupMember.objects.get_or_create(
            group=group, kind=d["kind"], **{field: target},
            defaults={"added_by": request.user},
        )
        serializer = self.get_serializer(group)
        return Response(serializer.data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=True, methods=["delete"], url_path="members/(?P<member_id>[^/.]+)")
    def remove_member(self, request, pk=None, member_id=None):
        """Remove one membership row. Scoped to this group so a member id from
        another tenant's group cannot be deleted by guessing it."""
        group = self.get_object()
        member = WorkflowApproverGroupMember.objects.filter(
            pk=member_id, group=group).first()
        if member is None:
            raise NotFound("Member not found.")
        member.delete()
        return Response(self.get_serializer(group).data)


# ── Stage approver overrides ──────────────────────────────────────────────────

class WorkflowStageApproverOverrideViewSet(TenantScopedMixin, ModelViewSet):
    """A tenant's own approver choices on stages it did not author.

    Central templates are published once and shared. Rather than cloning one to
    change a single approver, a tenant records an override here and the engine
    consults it at activation. Removing the override restores the template's
    own approver.

    docstring-name: Workflow stage approver overrides
    """
    serializer_class = WorkflowStageApproverOverrideSerializer

    def get_permissions(self):
        # Repointing an approval step is a template-level decision, so it takes
        # template manage rights rather than the lighter group rights.
        self.rbac_permission = (
            PERM_TEMPLATE_VIEW if self.action in ("list", "retrieve")
            else PERM_TEMPLATE_MANAGE
        )
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_serializer_context(self):
        return super().get_serializer_context() | {"tenant": self.get_tenant()}

    def get_queryset(self):
        qs = (WorkflowStageApproverOverride.all_objects
              .filter(tenant=self.get_tenant())
              .select_related("stage__template", "approver_group"))
        if self.request.query_params.get("document_type"):
            qs = qs.filter(
                stage__template__document_type=self.request.query_params["document_type"])
        return qs.order_by("stage__template__document_type", "stage__order")

    def perform_create(self, serializer):
        serializer.save(tenant=self.get_tenant(), created_by=self.request.user)


# ── Delegations ───────────────────────────────────────────────────────────────

class ApprovalDelegationViewSet(TenantScopedMixin, ModelViewSet):
    """docstring-name: Approval delegations"""
    serializer_class = ApprovalDelegationSerializer
    permission_classes = [IsAuthenticatedAndActive]

    def get_serializer_context(self):
        # The tenant every reference on the serializer resolves inside. Without
        # it the delegate would be looked up across the whole user table, which
        # is how a delegation could name somebody in another tenant.
        return super().get_serializer_context() | {"tenant": self.get_tenant()}

    def get_queryset(self):
        user = self.request.user
        qs = ApprovalDelegation.all_objects.filter(tenant=self.get_tenant())
        if not user_has_rbac_permission(user, PERM_TEMPLATE_MANAGE, tenant=user.tenant):
            # Non-admin users can only see delegations they created or receive.
            qs = qs.filter(Q(delegator=user) | Q(delegate=user))
        return qs.order_by("-starts_at")

    def perform_create(self, serializer):
        # Delegations are always created by the current user within the active
        # tenant scope. get_tenant() rather than request.tenant directly, so the
        # tenant the delegate was resolved inside and the tenant stored on the row
        # are the same expression and cannot drift into disagreeing.
        serializer.save(tenant=self.get_tenant(), delegator=self.request.user)

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        delegation = self.get_object()
        if (delegation.delegator_id != request.user.pk and
                not user_has_rbac_permission(request.user, PERM_TEMPLATE_MANAGE, tenant=request.tenant)):
            return Response({
                "success": False,
                "message": "You do not have permission to revoke this delegation.",
                "error": {"code": "PERMISSION_DENIED", "detail": {}},
            }, status=status.HTTP_403_FORBIDDEN)
        # Revocation is timestamped instead of deleting the delegation record.
        delegation.revoked_at = timezone.now()
        delegation.save(update_fields=["revoked_at"])
        return Response(ApprovalDelegationSerializer(delegation).data)
