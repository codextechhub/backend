"""REST views for vs_workflow. See urls.py for the full routing table."""

from collections import defaultdict

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, mixins
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from vs_notifications.services.acknowledge import acknowledge_record
from vs_notifications.services.routing import RecordFamily
from vs_rbac.permissions import (
    HasRBACPermission, IsAuthenticatedAndActive, is_vision_super_admin,
)
# ``include_shared=True`` spelled out at each call site below. A workflow row
# with no branch is shared across the school - a tenant-wide template, a group
# that approves for every site - and hiding those from a branch approver would
# break the branch -> tenant -> platform cascade the engine actually runs.
from vs_rbac.scoping import (
    assert_caller_may_change, assert_caller_may_configure, branch_q,
    caller_branch_ids, resolve_branch, sole_caller_branch,
)
from vs_tenants.models import Tenant
from vs_rbac.permissions import user_has_rbac_permission

from core.response import success_response

from vs_workflow.conditions.fields import document_type_label
from vs_workflow.exceptions import TemplateInvalidError, UnknownPositionError
from vs_workflow.constants import (
    PERM_TEMPLATE_PUBLISH, PERM_TEMPLATE_UPDATE,
    PERM_TEMPLATE_VIEW,
    PERM_INSTANCE_VIEW, PERM_INSTANCE_CANCEL,
    PERM_ACTION_REVERSE, PERM_GROUP_CREATE, PERM_GROUP_DELETE,
    PERM_GROUP_UPDATE, PERM_GROUP_VIEW, PERM_APPROVERS_ASSIGN,
    ApproverSource, GroupMemberKind, OrganogramTarget, StageKind,
    WorkflowInstanceStatus, WorkflowStageStatus,
)
from vs_workflow.models import (
    ApprovalDelegation, WorkflowApproverGroup, WorkflowApproverGroupMember,
    WorkflowDynamicRole, WorkflowInstance, WorkflowStage, WorkflowStageAction, WorkflowStageApproverOverride,
    WorkflowStageApprover, WorkflowStageInstance, WorkflowTemplate,
)
from vs_workflow.serializers import (
    ApprovalDelegationSerializer, ApproverPreviewRequestSerializer,
    DynamicRolePreviewSerializer, WorkflowDynamicRoleSerializer,
    CancelInstanceSerializer, ReverseActionSerializer,
    StageActionWriteSerializer,
    WorkflowApproverGroupMemberWriteSerializer, WorkflowApproverGroupSerializer,
    WorkflowStageApproverOverrideSerializer,
    WorkflowInstanceAdminRowSerializer, WorkflowInstanceDetailSerializer,
    WorkflowInstanceListSerializer, ReplaceApproverSerializer,
    StageApproversWriteSerializer, StageAssignmentResetSerializer,
    WorkflowTemplatePublishSerializer, WorkflowTemplateReadSerializer,
)
from vs_workflow.services import actions as actions_svc
from vs_workflow.services import comparison as comparison_svc
from vs_workflow.services import dynamic_roles as dynamic_roles_svc
from vs_workflow.services import my_queue as my_queue_svc
from vs_workflow.services import reassignment as reassignment_svc
from vs_workflow.services import release as release_svc
from vs_workflow.services import templates as templates_svc
from vs_workflow.services.approvers import (
    EligibleApprover, describe_group_members, resolve_approvers, resolve_group_users,
)
from vs_workflow.services.positions import bind_position
from vs_workflow.services.visibility import exclude_hidden_documents


# ── Helpers ───────────────────────────────────────────────────────────────────

# Who decides approvals is configuration like any other: a row with no branch
# binds every branch, and only a caller whose reach is the whole tenant may
# change it (vs_rbac.scoping.caller_may_change). These are the sentences a
# refused write carries, one pair per kind of row: the shared form, and the
# form for a row belonging to a branch the caller does not cover.
TEMPLATE_SHARED = (
    "Only a school-wide administrator can change the approval steps every "
    "branch follows. Ask one to change them, or publish them for your own branch."
)
TEMPLATE_OTHER_BRANCH = "You can only publish approval steps for your own branch."
GROUP_SHARED = (
    "Only a school-wide administrator can change this approver group, because "
    "it decides approvals for every branch. Ask one to change it, or create a "
    "group for your branch."
)
GROUP_OTHER_BRANCH = (
    "This approver group decides approvals for branches you do not work in, so "
    "only an administrator who covers them can change it."
)
DYNAMIC_ROLE_SHARED = (
    "Only a school-wide administrator can change a Dynamic Role, because it "
    "decides approvals for every branch. Ask one to change it."
)
STAGE_SHARED = (
    "Only a school-wide administrator can change who approves a step every "
    "branch follows. Ask one to change it."
)
STAGE_OTHER_BRANCH = (
    "You can only change who approves a step on your own branch's approval steps."
)
DELEGATION_NOT_YOURS = (
    "Only the person who handed over their approvals, or an administrator, can "
    "change this delegation."
)
DELEGATION_FOR_SOMEBODY_ELSE = (
    "Only an administrator who can change who approves requests can set up a "
    "delegation for somebody else."
)
DELEGATION_SHARED = (
    "This delegation belongs to somebody whose approvals reach beyond your "
    "branch, so only a school-wide administrator can change it."
)


def _assert_may_configure(request, branch, *, shared, other_branch):
    """Refuse a write to workflow configuration owned by *branch* past the caller's reach (403).

    ``branch`` is ``None`` for a row the whole tenant shares.
    """
    assert_caller_may_configure(
        request.user, request.tenant, branch,
        message=shared if branch is None else other_branch,
    )


def _branch_for_new_row(request, named):
    """The branch a new template or group is for, when the caller names none.

    The one named, else the caller's own branch when they work in exactly
    one, else the whole tenant. A whole-tenant caller naming none writes the
    tenant's own row, so their home posting never decides it. A caller bound
    to several branches who names none is asking for the whole tenant, which
    :func:`_assert_may_configure` then refuses.
    """
    if named is not None:
        return named
    if caller_branch_ids(request) is None:
        return None
    return sole_caller_branch(request, request.tenant)


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


# Resolve tenant context once for all workflow views.
class TenantScopedMixin:
    """Single source of truth for "which tenant is this request about".

    ``request.tenant`` is resolved by TenantJWTAuthentication from the asserted
    ``?tenant=`` and is always present on an authenticated request. Querysets
    scope by it explicitly rather than trusting the ambient context a
    tenant-aware manager reads, because some workflow models (stage instances,
    stage actions) have no such manager and would otherwise not be scoped at
    all.

    Branch scope is not here. Whose rows a caller sees comes from
    :func:`vs_rbac.scoping.branch_q`, and which branch a new template or group
    is for from :func:`_branch_for_new_row`; the caller's home posting
    decides neither.
    """

    def get_tenant(self):
        return getattr(self.request, "tenant", None)


# ── Templates ────────────────────────────────────────────────────────────────

class WorkflowTemplateViewSet(
    TenantScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet,
):
    """docstring-name: Workflow templates"""
    serializer_class = WorkflowTemplateReadSerializer

    def get_permissions(self):
        self.rbac_permission = {
            "publish": PERM_TEMPLATE_PUBLISH,
            "use_platform_version": PERM_TEMPLATE_UPDATE,
        }.get(self.action, PERM_TEMPLATE_VIEW)
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
        #
        # That rule was right and was hand-written here, but it was keyed off
        # ``request.user.branch`` - one column, which cannot say "Ikeja and Lekki
        # but not Yaba", and which is null for most school users so it narrowed
        # nothing at all. It is now the platform's own predicate, keyed off the
        # grants that also decide whether this screen opens, so the gate and the
        # narrowing cannot answer differently.
        qs = qs.filter(branch_q(self.request, include_shared=True))
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
        # Inside the caller's tenant: resolution runs in the requester's
        # tenant, so any other id would answer with somebody else's approvers.
        try:
            requester = UserModel.objects.filter(
                pk=d["requester"], tenant=request.tenant).first()
        except (ValueError, TypeError):
            requester = None
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
            # Looked up on the chart the caller's tenant uses, as publish does.
            try:
                bound = bind_position(d["organogram_position_code"], request.tenant,
                                      active_only=False)
            except UnknownPositionError as exc:
                return Response({"detail": exc.message}, status=status.HTTP_404_NOT_FOUND)
            stage.organogram_position = bound.position
            stage.organogram_tenant_position_id = bound.tenant_position_id
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
        if d["approver_source"] == ApproverSource.DYNAMIC_ROLE and d.get("dynamic_role_code"):
            # A saved Dynamic Role, tried with the same checks and matching the
            # engine runs, against the caller's own tenant only.
            dynamic_role = WorkflowDynamicRole.all_objects.filter(
                tenant=request.tenant, code=d["dynamic_role_code"], is_active=True,
            ).first()
            if dynamic_role is None:
                return Response(
                    {"detail": f"No active Dynamic Role with code "
                               f"'{d['dynamic_role_code']}' exists in this tenant."},
                    status=status.HTTP_404_NOT_FOUND)
            branch = instance.branch if stage.approver_scope == "BRANCH" else None
            try:
                users, rule_preview = dynamic_roles_svc.preview(
                    tenant=request.tenant, requester=requester,
                    document_types=dynamic_role.document_types,
                    rules=dynamic_roles_svc.rules_as_payload(dynamic_role),
                    sample=d.get("sample"), branch=branch)
            except TemplateInvalidError as exc:
                return Response({"detail": exc.message},
                                status=status.HTTP_400_BAD_REQUEST)
            eligible = [EligibleApprover(user=user) for user in users]
        elif d["approver_source"] == ApproverSource.DYNAMIC_ROLE:
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
        """Create or replace one set of approval steps.

        ``branch`` names the branch the steps are for. Left out, they are the
        caller's own branch's when the caller works in exactly one, and the
        tenant's own otherwise. Steps for the whole tenant need a caller whose
        reach is the whole tenant, and a branch's steps need that branch: a
        refusal is 403 ``SHARED_RECORD_READ_ONLY`` and nothing is written.
        """
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

        # A shared template belongs to no branch; carrying the publisher's
        # own branch would scope it out of every tenant that inherits it.
        branch = None
        if not as_platform:
            branch = _branch_for_new_row(
                request, resolve_branch(request.tenant, d.get("branch")),
            )
            _assert_may_configure(
                request, branch,
                shared=TEMPLATE_SHARED, other_branch=TEMPLATE_OTHER_BRANCH,
            )

        # Template publishing replaces stage/route configuration through the service layer.
        t = templates_svc.publish_template(
            tenant=None if as_platform else request.tenant,
            branch=branch,
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
        if template.tenant_id is not None:
            _assert_may_configure(
                request, template.branch,
                shared=TEMPLATE_SHARED, other_branch=TEMPLATE_OTHER_BRANCH,
            )
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
    """Read and act on approval instances. Read-only as a collection, by design.

    There is deliberately no generic ``POST /instances/``. One used to exist,
    taking a content type id and an object id and loading the document with its
    ordinary manager - which meant it could load any tenant's row, and
    ``document_scope`` then filed the instance under the *document's* tenant.
    A bursar at one school could submit another school's payout batch, read the
    amount and the approver names back out of the 201, and leave the batch
    marked pending approval.

    A generic submitter cannot be fixed cheaply, because "which rows may this
    caller see" is a question only the owning module can answer. So submission
    stays where that answer already lives: each module exposes its own submit
    endpoint over its own scoped queryset (see
    ``vs_procurement.views.requisitions``, ``vs_finance.views_ar``,
    ``vs_payments.views``), and they all funnel into
    ``services.submission.submit_for_approval``, which refuses to file into a
    tenant the submitter does not belong to.

    docstring-name: Workflow instances
    """
    def get_permissions(self):
        if self.action == "cancel":
            self.rbac_permission = PERM_INSTANCE_CANCEL
        elif self.action in ("list", "filter_options"):
            self.rbac_permission = PERM_INSTANCE_VIEW
        elif self.action in ("approvers", "reset_approvers", "replace_approver"):
            self.rbac_permission = PERM_APPROVERS_ASSIGN
        elif self.action == "retrieve":
            # Object-level access below also admits the requester and the frozen
            # approver snapshot. Those people need the decision surface without
            # being granted the tenant-wide approval-history permission.
            return [IsAuthenticatedAndActive()]
        else:
            # Actor-level actions are guarded by ownership/eligibility in the service layer.
            return [IsAuthenticatedAndActive()]
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return WorkflowInstanceDetailSerializer
        if self.action == "list":
            return WorkflowInstanceAdminRowSerializer
        return WorkflowInstanceListSerializer

    def _reachable(self):
        """Every request this caller may reach by id: their tenant, their branches, visible documents.

        The one scope every read and write on this viewset starts from, so an
        administrator can never reach another tenant's request, or one outside
        their branches, by naming its id.
        """
        qs = WorkflowInstance.all_objects.filter(
            branch_q(self.request, include_shared=True), tenant=self.get_tenant(),
        )
        # A document its own module keeps from this reader is absent here too, so
        # its detail, approve and reject answer 404.
        return exclude_hidden_documents(qs, self.request.user, self.get_tenant())

    def get_queryset(self):
        qs = (self._reachable()
              .select_related("template", "current_stage")
              .order_by("-updated_at", "-created_at"))
        if self.action == "retrieve":
            # The stage history, and names for the timeline's "who did it" labels.
            qs = qs.prefetch_related(
                "stage_instances__stage", "stage_instances__eligible_approvers",
                "stage_instances__actions__actor", "stage_instances__actions__proxied_by",
                "audit_logs__actor", "audit_logs__effective_user",
            )
        elif self.action == "list":
            qs = self._filter_list(self._with_waiting_on(qs))
        return qs

    @staticmethod
    def _with_waiting_on(qs):
        """Everything the administrators' row reads, fetched once per page."""
        from django.db.models import Prefetch

        live_actions = WorkflowStageAction.objects.filter(
            reversed_at__isnull=True, is_reversal_of__isnull=True,
        ).only("id", "stage_instance_id", "actor_id", "proxied_by_id", "attempt")
        waiting = (WorkflowStageInstance.objects
                   .filter(status=WorkflowStageStatus.ACTIVE)
                   .prefetch_related(
                       Prefetch("eligible_approvers",
                                queryset=WorkflowStageApprover.objects
                                .select_related("user", "on_behalf_of")
                                .order_by("recorded_at", "pk")),
                       Prefetch("actions", queryset=live_actions, to_attr="live_actions"),
                   ))
        approval_stages = (WorkflowStage.objects
                           .filter(kind=StageKind.APPROVAL, retired_at__isnull=True)
                           .order_by("order", "pk").only("id", "template_id", "order"))
        return qs.select_related("branch", "request_for").prefetch_related(
            Prefetch("template__stages", queryset=approval_stages, to_attr="approval_stages"),
            Prefetch("stage_instances", queryset=waiting, to_attr="waiting_stage_instances"),
        )

    def _filter_list(self, qs):
        """Apply the list's query parameters. Each is optional and they combine with AND.

        An unreadable value (a date that is not one, a count of days that is
        not a whole number) is refused with a 400 naming the parameter, rather
        than silently ignored and answered with every request.
        """
        from datetime import date, datetime, time, timedelta

        from django.db.models import Exists, F, OuterRef
        from rest_framework.exceptions import ValidationError

        from vs_config.clock import tenant_zone

        p = self.request.query_params

        def whole(name, minimum=0):
            try:
                value = int(p[name])
            except (TypeError, ValueError):
                raise ValidationError({name: "Give a whole number."})
            if value < minimum:
                raise ValidationError({name: f"Give a whole number of at least {minimum}."})
            return value

        def day(name):
            try:
                return date.fromisoformat(p[name])
            except ValueError:
                raise ValidationError({name: "Give a date as YYYY-MM-DD."})

        if p.get("document_type"):
            qs = qs.filter(document_type=p["document_type"])
        if p.get("status"):
            statuses = [s.strip() for s in p["status"].split(",") if s.strip()]
            qs = qs.filter(status__in=statuses)
        if p.get("requested_by"):
            qs = qs.filter(requested_by_id=whole("requested_by", 1))
        if p.get("request_for"):
            qs = qs.filter(request_for_id=whole("request_for", 1))
        if p.get("template_code"):
            qs = qs.filter(template__code=p["template_code"])
        if p.get("stage"):
            qs = qs.filter(current_stage_id=p["stage"])
        if p.get("branch"):
            qs = qs.filter(branch_id=whole("branch", 1))
        if p.get("search"):
            term = p["search"].strip()
            qs = qs.filter(
                Q(document_summary__title__icontains=term)
                | Q(document_object_id__icontains=term)
                | Q(pk=term)
            )
        if p.get("waiting_on"):
            user_id = whole("waiting_on", 1)
            voted = WorkflowStageAction.objects.filter(
                Q(actor_id=user_id) | Q(proxied_by_id=user_id),
                stage_instance=OuterRef("stage_instance"), attempt=OuterRef("attempt"),
                reversed_at__isnull=True, is_reversal_of__isnull=True,
            )
            places = (WorkflowStageApprover.objects
                      .filter(user_id=user_id,
                              stage_instance__status=WorkflowStageStatus.ACTIVE,
                              attempt=F("stage_instance__attempt"))
                      .exclude(Exists(voted))
                      .values("stage_instance__instance_id"))
            qs = qs.filter(status=WorkflowInstanceStatus.IN_PROGRESS, pk__in=places)
        if p.get("waiting_longer_than"):
            opened_before = timezone.now() - timedelta(days=whole("waiting_longer_than"))
            qs = qs.filter(
                Exists(WorkflowStageInstance.objects.filter(
                    instance=OuterRef("pk"), stage=OuterRef("current_stage"),
                    status=WorkflowStageStatus.ACTIVE, activated_at__lt=opened_before,
                )),
                status=WorkflowInstanceStatus.IN_PROGRESS,
            )
        if p.get("submitted_from") or p.get("submitted_to"):
            zone = tenant_zone(self.get_tenant())
            if p.get("submitted_from"):
                start = datetime.combine(day("submitted_from"), time.min, tzinfo=zone)
                qs = qs.filter(submitted_at__gte=start)
            if p.get("submitted_to"):
                end = datetime.combine(day("submitted_to") + timedelta(days=1), time.min,
                                       tzinfo=zone)
                qs = qs.filter(submitted_at__lt=end)
        return qs

    def retrieve(self, request, *args, **kwargs):
        """Return the instance, and clear the bell entries about it.

        One instance reaches two audiences through two destinations, an
        approver's queue and a submitter's own list, and reading the instance
        clears both: whoever is asking has just seen the state the notice was
        announcing. Only a successful read acknowledges, so a caller refused by
        the tenant scope clears nothing.
        """
        instance = self.get_object()
        if not self._may_read_instance(request, instance):
            raise NotFound("Workflow instance not found.")
        response = Response(self.get_serializer(instance).data)
        acknowledge_record(
            request.user,
            family=RecordFamily.WORKFLOW_INSTANCE,
            value=kwargs.get("pk"),
        )
        return response

    @staticmethod
    def _may_read_instance(request, instance) -> bool:
        """Admit a participant or a tenant-wide workflow-history viewer.

        The eligible-approver rows are the frozen authority the action service
        checks, including delegated approvals. Reading that same snapshot here
        keeps the evidence and the decision available to the same people. A 404
        hides instance existence from authenticated non-participants.
        """
        user = request.user
        if instance.requested_by_id == user.pk or is_vision_super_admin(user):
            return True
        if user_has_rbac_permission(user, PERM_INSTANCE_VIEW, tenant=instance.tenant):
            return True
        return WorkflowStageApprover.objects.filter(
            stage_instance__instance=instance,
            user=user,
        ).exists()

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        instance = actions_svc.withdraw(self.get_object().id, request.user)
        return Response(WorkflowInstanceDetailSerializer(
            instance, context=self.get_serializer_context(),
        ).data)

    @action(detail=True, methods=["post"])
    def resubmit(self, request, pk=None):
        instance = actions_svc.resubmit(self.get_object().id, request.user)
        return Response(WorkflowInstanceDetailSerializer(
            instance, context=self.get_serializer_context(),
        ).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        p = CancelInstanceSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        instance = actions_svc.cancel(
            self.get_object().id, request.user, p.validated_data["reason"])
        return Response(WorkflowInstanceDetailSerializer(
            instance, context=self.get_serializer_context(),
        ).data)

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
        if not release_svc.handler_allows_release(instance):
            return Response({
                "success": False,
                "message": "This document type requires a human approval and cannot continue without one.",
                "error": {
                    "code": "CONTINUE_WITHOUT_APPROVAL_NOT_ALLOWED",
                    "detail": release_svc.describe_park(instance),
                },
            }, status=status.HTTP_409_CONFLICT)
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
        except release_svc.ReleaseNotAllowedError as exc:
            return Response({
                "success": False,
                "message": str(exc),
                "error": {
                    "code": "CONTINUE_WITHOUT_APPROVAL_NOT_ALLOWED",
                    "detail": release_svc.describe_park(instance),
                },
            }, status=status.HTTP_409_CONFLICT)
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
        return Response(WorkflowInstanceDetailSerializer(
            instance, context=self.get_serializer_context(),
        ).data)

    @action(detail=True, methods=["post"], url_path="actions")
    def record_action(self, request, pk=None):
        p = StageActionWriteSerializer(data=request.data)
        p.is_valid(raise_exception=True)
        instance = actions_svc.record_action(
            self.get_object().id, request.user,
            action=p.validated_data["action"],
            comment=p.validated_data.get("comment", ""),
        )
        return Response(WorkflowInstanceDetailSerializer(
            instance, context=self.get_serializer_context(),
        ).data)

    @action(detail=False, methods=["get"], url_path="filter-options")
    def filter_options(self, request):
        """GET - the stages a request of one document type can be waiting on, for the list's filter.

        The approval stages of the templates this tenant runs that type under:
        its own active templates within the caller's branches, the shared
        template where the tenant has not adjusted it, and any template one of
        its requests is still running on. Empty when no ``document_type`` is
        given.

        docstring-name: Approval request filter options
        """
        document_type = request.query_params.get("document_type") or ""
        if not document_type:
            return success_response(data={"stages": []})
        tenant = self.get_tenant()
        own = (WorkflowTemplate.all_objects
               .filter(branch_q(request, include_shared=True),
                       tenant=tenant, document_type=document_type, is_active=True))
        own_codes = set(own.values_list("code", flat=True))
        shared = (WorkflowTemplate.all_objects
                  .filter(tenant__isnull=True, document_type=document_type, is_active=True)
                  .exclude(code__in=own_codes))
        running = self._reachable().filter(document_type=document_type).values("template_id")
        template_ids = (set(own.values_list("pk", flat=True))
                        | set(shared.values_list("pk", flat=True))
                        | set(running.values_list("template_id", flat=True)))
        stages = (WorkflowStage.objects
                  .filter(template_id__in=template_ids, kind=StageKind.APPROVAL)
                  .select_related("template")
                  .order_by("template__code", "order", "pk"))
        return success_response(data={"stages": [
            {"id": str(s.pk), "label": s.label, "template_code": s.template.code}
            for s in stages
        ]})

    @action(detail=True, methods=["get", "post"])
    def approvers(self, request, pk=None):
        """GET, POST - who approves each stage of this request, and change it.

        GET lists every approval stage: those decided, the one waiting, and
        those still to come, with the change history. POST sets the complete
        list of people for one stage (``stage``, ``approvers``, ``reason``):
        the waiting stage changes now, a stage still to come keeps the choice
        for when it opens. Answers with the same body as GET. Refusals carry
        ``APPROVER_ALREADY_VOTED``, ``APPROVER_CONFLICT``,
        ``APPROVER_OUT_OF_REACH``, ``STAGE_EMPTY``, ``STAGE_NOT_CHANGEABLE``,
        ``INSTANCE_NOT_OPEN`` or ``REASON_REQUIRED``.

        docstring-name: Approval request approvers
        """
        instance = self.get_object()
        if request.method == "POST":
            body = StageApproversWriteSerializer(data=request.data)
            body.is_valid(raise_exception=True)
            reassignment_svc.set_stage_approvers(
                instance.pk, stage_id=body.validated_data["stage"],
                user_ids=body.validated_data["approvers"],
                reason=body.validated_data["reason"], actor=request.user,
            )
            instance = self._reachable().get(pk=instance.pk)
        return success_response(data=reassignment_svc.approver_overview(instance))

    @action(detail=True, methods=["post"], url_path="approvers/reset")
    def reset_approvers(self, request, pk=None):
        """POST - drop a stage's advance choice of approvers, so it resolves normally when it opens.

        docstring-name: Reset a stage's approvers
        """
        instance = self.get_object()
        body = StageAssignmentResetSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reassignment_svc.reset_stage_assignment(
            instance.pk, stage_id=body.validated_data["stage"],
            reason=body.validated_data["reason"], actor=request.user,
        )
        instance = self._reachable().get(pk=instance.pk)
        return success_response(data=reassignment_svc.approver_overview(instance))

    @action(detail=False, methods=["post"], url_path="replace-approver")
    def replace_approver(self, request):
        """POST - hand everything waiting on one person to another, across many requests.

        Each request is changed on its own: one that cannot be changed (the
        person has already decided it, the new person cannot approve it, it is
        out of the caller's reach) is skipped with the reason, never failing
        the rest.

        docstring-name: Replace an approver across requests
        """
        body = ReplaceApproverSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        ids = [str(i) for i in body.validated_data["instance_ids"]]
        reachable = set(self._reachable().filter(pk__in=ids).values_list("pk", flat=True))
        outcome = reassignment_svc.replace_approver(
            ids, from_user_id=body.validated_data["from_user"],
            to_user_id=body.validated_data["to_user"],
            reason=body.validated_data["reason"], actor=request.user,
            tenant=self.get_tenant(), reachable_ids=reachable,
        )
        return success_response(data=outcome)


class WorkflowNotificationSettingView(APIView):
    """GET, PATCH /v1/workflow/notification-settings/

    One switch for the whole school: whether its approvals notify anybody.
    Reading needs only template view, because the Workflow area shows the
    current answer; changing it needs template update, the same key that
    decides who approves what, and a caller whose reach is the whole tenant,
    because the switch covers every branch. A branch-bound caller holding the
    key is refused with a 403 (SHARED_RECORD_READ_ONLY) and nothing is written.

    docstring-name: Workflow notifications
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    def get_permissions(self):
        self.rbac_permission = (
            PERM_TEMPLATE_UPDATE if self.request.method == "PATCH" else PERM_TEMPLATE_VIEW
        )
        return super().get_permissions()

    def get(self, request):
        from vs_workflow.services.notification_settings import notifications_enabled

        return Response({"enabled": notifications_enabled(request.tenant)})

    def patch(self, request):
        from vs_rbac.scoping import assert_caller_may_configure
        from vs_workflow.services.notification_settings import set_notifications_enabled

        assert_caller_may_configure(
            request.user, request.tenant,
            message=(
                "Only a school-wide administrator can change whether approvals "
                "send notifications."
            ),
        )
        enabled = request.data.get("enabled")
        if not isinstance(enabled, bool):
            return Response(
                {"enabled": "Say true or false."}, status=status.HTTP_400_BAD_REQUEST,
            )
        set_notifications_enabled(request.tenant, request.user, enabled=enabled)
        return Response({"enabled": enabled})


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
            {"document_type": dt, "document_type_label": document_type_label(dt),
             "stage_code": code, "stage_label": info["stage_label"],
             "active_count": info["count"]}
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
        # group still takes the group's own action-specific key.
        self.rbac_permission = {
            "create": PERM_GROUP_CREATE,
            "destroy": PERM_GROUP_DELETE,
            "update": PERM_GROUP_UPDATE,
            "partial_update": PERM_GROUP_UPDATE,
            "add_member": PERM_GROUP_UPDATE,
            "remove_member": PERM_GROUP_UPDATE,
        }.get(self.action, [PERM_GROUP_VIEW, PERM_TEMPLATE_UPDATE])
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_serializer_context(self):
        return super().get_serializer_context() | {"tenant": self.request.tenant}

    def get_queryset(self):
        qs = (WorkflowApproverGroup.all_objects
              .filter(branch_q(self.request, include_shared=True),
                      tenant=self.get_tenant())
              .prefetch_related("members__user", "members__role", "members__position"))
        if self.request.query_params.get("is_active") in ("true", "false"):
            qs = qs.filter(is_active=self.request.query_params["is_active"] == "true")
        if self.request.query_params.get("search"):
            term = self.request.query_params["search"]
            qs = qs.filter(Q(name__icontains=term) | Q(code__icontains=term))
        return qs.order_by("name")

    def _assert_may_change(self, group):
        """Refuse a change to *group* unless the caller covers every branch it approves for (403)."""
        from vs_workflow.services.groups import group_branch_ids

        ids = group_branch_ids(group)
        assert_caller_may_change(
            self.request.user, self.request.tenant, ids,
            message=GROUP_OTHER_BRANCH if ids else GROUP_SHARED,
        )

    def perform_create(self, serializer):
        branch = _branch_for_new_row(
            self.request, serializer.validated_data.get("branch"),
        )
        _assert_may_configure(
            self.request, branch, shared=GROUP_SHARED, other_branch=GROUP_OTHER_BRANCH,
        )
        serializer.save(
            tenant=self.request.tenant, created_by=self.request.user, branch=branch,
        )

    def perform_update(self, serializer):
        self._assert_may_change(serializer.instance)
        if "branch" in serializer.validated_data:
            _assert_may_configure(
                self.request, serializer.validated_data["branch"],
                shared=GROUP_SHARED, other_branch=GROUP_OTHER_BRANCH,
            )
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        """Refuse to delete a group a template still points at.

        The FK is PROTECT, so the alternative is a 500. Deactivating keeps the
        stage resolvable (to nobody) and preserves audit history.
        """
        group = self.get_object()
        self._assert_may_change(group)
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
        self._assert_may_change(group)
        s = WorkflowApproverGroupMemberWriteSerializer(
            data=request.data, context={"tenant": request.tenant})
        s.is_valid(raise_exception=True)
        d = s.validated_data
        target = d["resolved_target"]
        if d["kind"] == GroupMemberKind.POSITION:
            # A post on the CX chart, or one on this tenant's own; never both.
            reference = ({"position": target.position} if target.position is not None
                         else {"tenant_position_id": target.tenant_position_id})
        else:
            field = {GroupMemberKind.USER: "user", GroupMemberKind.ROLE: "role"}[d["kind"]]
            reference = {field: target}

        member, created = WorkflowApproverGroupMember.objects.get_or_create(
            group=group, kind=d["kind"], **reference,
            defaults={"added_by": request.user},
        )
        # Read again: the group above carries its members as prefetched before the add.
        serializer = self.get_serializer(self.get_object())
        return Response(serializer.data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=True, methods=["delete"], url_path="members/(?P<member_id>[^/.]+)")
    def remove_member(self, request, pk=None, member_id=None):
        """Remove one membership row. Scoped to this group so a member id from
        another tenant's group cannot be deleted by guessing it."""
        group = self.get_object()
        self._assert_may_change(group)
        member = WorkflowApproverGroupMember.objects.filter(
            pk=member_id, group=group).first()
        if member is None:
            raise NotFound("Member not found.")
        member.delete()
        # Read again: the group above carries its members as prefetched before the delete.
        return Response(self.get_serializer(self.get_object()).data)


# ── Dynamic Roles ─────────────────────────────────────────────────────────────

class WorkflowDynamicRoleViewSet(TenantScopedMixin, ModelViewSet):
    """Named Dynamic Roles behind the Approvers screen's Dynamic Role tab.

    docstring-name: Workflow Dynamic Roles
    """
    serializer_class = WorkflowDynamicRoleSerializer

    _WRITE_ACTIONS = {"create", "update", "partial_update", "destroy"}

    def get_permissions(self):
        # The approver-group keys: both answer "who approves" on one screen, and
        # a template builder picking a Dynamic Role has to be able to read them.
        # Trying rules writes nothing, so reading is enough for the preview.
        self.rbac_permission = {
            "create": PERM_GROUP_CREATE,
            "destroy": PERM_GROUP_DELETE,
            "update": PERM_GROUP_UPDATE,
            "partial_update": PERM_GROUP_UPDATE,
        }.get(self.action, [PERM_GROUP_VIEW, PERM_TEMPLATE_UPDATE])
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_serializer_context(self):
        return super().get_serializer_context() | {"tenant": self.request.tenant}

    def get_queryset(self):
        qs = (WorkflowDynamicRole.all_objects
              .filter(tenant=self.get_tenant())
              .prefetch_related("rules__role", "rules__user", "rules__group"))
        params = self.request.query_params
        if params.get("is_active") in ("true", "false"):
            qs = qs.filter(is_active=params["is_active"] == "true")
        if params.get("search"):
            term = params["search"]
            qs = qs.filter(Q(name__icontains=term) | Q(code__icontains=term))
        if params.get("document_type"):
            # Serves this type, or serves any.
            qs = qs.filter(Q(document_types__contains=[params["document_type"]])
                           | Q(document_types=[]))
        return qs.order_by("name")

    def _assert_may_change(self):
        """A Dynamic Role serves every branch, so only a whole-tenant caller changes one (403)."""
        assert_caller_may_configure(
            self.request.user, self.request.tenant, message=DYNAMIC_ROLE_SHARED,
        )

    def perform_create(self, serializer):
        self._assert_may_change()
        serializer.save(tenant=self.request.tenant, created_by=self.request.user)

    def perform_update(self, serializer):
        self._assert_may_change()
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        """Refuse to delete a Dynamic Role any stage still points at.

        A stage's link is PROTECT, so the alternative is a 500. Deactivating
        keeps those stages resolvable - to nobody, so they park - and keeps the
        audit history readable.
        """
        dynamic_role = self.get_object()
        self._assert_may_change()
        used_by = list(dynamic_role.workflow_stages
                       .order_by("retired_at", "template__code")
                       .values_list("template__code", "code")[:10])
        if used_by:
            return Response({
                "success": False,
                "message": "This Dynamic Role is used by one or more workflow stages. "
                           "Deactivate it instead, or point those stages elsewhere first.",
                "error": {
                    "code": "DYNAMIC_ROLE_IN_USE",
                    "detail": {"stages": [f"{t}:{s}" for t, s in used_by]},
                },
            }, status=status.HTTP_409_CONFLICT)
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=["get"])
    def fields(self, request):
        """What a Dynamic Role serving ``?document_type=`` may test, and who it may send to.

        A Dynamic Role names no document type - a stage does, when it picks the
        role - so asking without ``document_type`` returns the whole catalogue
        for the caller's tenant: every area, and every field in it. Each field
        says which document types can answer it, and publishing refuses a stage
        whose document cannot answer a rule it would run.

        ``areas`` are the parts of the school a condition can ask about, in the
        order a screen should offer them: this document, the person who raised
        it, and whatever a domain app has declared, such as the child a bill is
        for. ``document_types`` lists the types the tenant raises, and
        ``approver_roles`` the roles a rule may send to - approving roles only,
        since the engine nominates no other.
        """
        from vs_rbac.models import TenantRoleTemplate
        from vs_workflow.conditions.fields import areas_for, catalogue
        from vs_workflow.handlers.registry import handlers_raised_by

        try:
            requested = dynamic_roles_svc.check_document_types(
                request.tenant, request.query_params.getlist("document_type"))
        except TemplateInvalidError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        # Narrowed to what was asked for, else everything this tenant raises.
        scope = requested or sorted(handlers_raised_by(request.tenant))
        # Some facts belong to one kind of tenant: a job title is read from a
        # school's staff record, and a platform operator has no such record, so
        # offering it here would offer a question with no answer behind it.
        kind = getattr(request.tenant, "kind", None)
        roles = TenantRoleTemplate.objects.filter(
            tenant=request.tenant, status=TenantRoleTemplate.Status.ACTIVE,
            is_system_role=True,
        ).order_by("name")
        return Response({
            "areas": [area.as_dict() for area in areas_for(scope, tenant_kind=kind)],
            "fields": [field.as_dict() for field in catalogue(scope, tenant_kind=kind)],
            "document_types": [
                {"value": t, "label": document_type_label(t)}
                for t in sorted(handlers_raised_by(request.tenant))
            ],
            "approver_roles": [{"key": role.key, "name": role.name} for role in roles],
        })

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """Who unsaved rules would choose for a requester and a sample document.

        Runs the checks saving runs, so a rule that would not save answers 400
        with the same reason. The requester is looked up inside the caller's
        tenant, since both the facts the rules read and the approvers come from
        it.
        """
        s = DynamicRolePreviewSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        from django.contrib.auth import get_user_model
        try:
            requester = get_user_model().objects.filter(
                pk=d["requester"], tenant=request.tenant).first()
        except (ValueError, TypeError):
            requester = None
        if requester is None:
            return Response({"detail": "Requester not found."}, status=status.HTTP_404_NOT_FOUND)
        branch = None
        if d.get("branch"):
            from vs_tenants.references import find_branch_in_tenant
            branch = find_branch_in_tenant(request.tenant, d["branch"])
            if branch is None:
                return Response({"detail": "Branch not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            users, detail = dynamic_roles_svc.preview(
                tenant=request.tenant, requester=requester,
                document_types=d["document_types"], rules=d["rules"],
                sample=d["sample"], branch=branch)
        except TemplateInvalidError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        approvers = [
            {"user": {"id": str(u.pk),
                      "full_name": getattr(u, "full_name", "") or u.get_username(),
                      "email": getattr(u, "email", "")}}
            for u in users
        ]
        return Response({"count": len(approvers), "approvers": approvers,
                         "dynamic_role": detail})


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
        # template update rights rather than the lighter group rights.
        self.rbac_permission = {
            "create": PERM_TEMPLATE_UPDATE,
            "destroy": PERM_TEMPLATE_UPDATE,
            "update": PERM_TEMPLATE_UPDATE,
            "partial_update": PERM_TEMPLATE_UPDATE,
        }.get(self.action, PERM_TEMPLATE_VIEW)
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

    def _assert_may_change(self, stage):
        """Refuse repointing *stage* unless the caller covers the branch its steps are for (403).

        A stage on a shared template, or on the tenant's own, binds every
        branch; one on a branch's own template binds that branch.
        """
        _assert_may_configure(
            self.request, stage.template.branch,
            shared=STAGE_SHARED, other_branch=STAGE_OTHER_BRANCH,
        )

    def perform_create(self, serializer):
        self._assert_may_change(serializer.validated_data["stage"])
        serializer.save(tenant=self.get_tenant(), created_by=self.request.user)

    def perform_update(self, serializer):
        self._assert_may_change(serializer.instance.stage)
        if "stage" in serializer.validated_data:
            self._assert_may_change(serializer.validated_data["stage"])
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_may_change(instance.stage)
        instance.delete()


# ── Delegations ───────────────────────────────────────────────────────────────

class ApprovalDelegationViewSet(TenantScopedMixin, ModelViewSet):
    """Hand one's approvals to somebody else for a period.

    Open to every signed-in member of the tenant, since anybody who approves
    may be away. ``document-types/`` lists what a delegation may be narrowed
    to, by name, so the person filling the form picks "Leave request" rather
    than typing ``leave.request``.

    An administrator (``workflow.template.update`` or
    ``workflow.approvers.assign``) sees every delegation in the tenant and may
    revoke any of them for a person within their branches; with
    ``workflow.approvers.assign`` they may also create one on somebody else's
    behalf by naming the ``delegator``. A delegation that starts at once
    reaches the requests already waiting on its delegator as it is created,
    and the create response says how many (``applied_to_waiting``); one that
    starts later reaches them when it starts. Revoking one takes its delegate
    off the requests still waiting on them.

    docstring-name: Approval delegations
    """
    serializer_class = ApprovalDelegationSerializer
    permission_classes = [IsAuthenticatedAndActive]

    def get_serializer_context(self):
        # The tenant every reference on the serializer resolves inside. Without
        # it the delegate would be looked up across the whole user table, which
        # is how a delegation could name somebody in another tenant.
        return super().get_serializer_context() | {"tenant": self.get_tenant()}

    def _is_delegation_admin(self) -> bool:
        user, tenant = self.request.user, self.get_tenant()
        return any(
            user_has_rbac_permission(user, key, tenant=tenant)
            for key in (PERM_TEMPLATE_UPDATE, PERM_APPROVERS_ASSIGN)
        )

    def get_queryset(self):
        user = self.request.user
        qs = (ApprovalDelegation.all_objects.filter(tenant=self.get_tenant())
              .select_related("created_by"))
        if not self._is_delegation_admin():
            # Non-admin users can only see delegations they created or receive.
            qs = qs.filter(Q(delegator=user) | Q(delegate=user))
        return qs.order_by("-starts_at")

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        response.data["applied_to_waiting"] = self._applied_to_waiting
        return response

    def perform_create(self, serializer):
        """Save the delegation, and carry it at once to the requests waiting on its delegator.

        The caller is the delegator unless they name somebody else, which needs
        ``workflow.approvers.assign`` and a delegator whose approvals stay
        within the caller's branches. get_tenant() rather than request.tenant
        directly, so the tenant the delegate was resolved inside and the tenant
        stored on the row are the same expression and cannot drift into
        disagreeing.
        """
        from rest_framework.exceptions import PermissionDenied
        from vs_rbac.grant_reach import assert_caller_may_change_person

        user = self.request.user
        delegator = serializer.validated_data.get("delegator") or user
        if delegator.pk != user.pk:
            if not user_has_rbac_permission(user, PERM_APPROVERS_ASSIGN, tenant=self.get_tenant()):
                raise PermissionDenied(DELEGATION_FOR_SOMEBODY_ELSE)
            assert_caller_may_change_person(
                user, self.get_tenant(), delegator, message=DELEGATION_SHARED,
            )
        with transaction.atomic():
            delegation = serializer.save(
                tenant=self.get_tenant(), delegator=delegator, created_by=user,
            )
            now = timezone.now()
            self._applied_to_waiting = 0
            if delegation.starts_at <= now <= delegation.ends_at:
                self._applied_to_waiting = reassignment_svc.apply_delegation(
                    delegation, actor=user, now=now,
                )

    @action(detail=False, methods=["get"], url_path="document-types")
    def document_types(self, request):
        """GET /workflow/delegations/document-types/ - what a delegation can cover.

        The document types this tenant raises, each with the name screens use,
        ordered by that name. A delegation for any other type would never apply
        to anything, so the serializer refuses one.
        """
        from vs_workflow.handlers.registry import handlers_raised_by

        rows = [
            {"value": t, "label": document_type_label(t)}
            for t in handlers_raised_by(self.get_tenant())
        ]
        return Response(sorted(rows, key=lambda row: row["label"]))

    def _assert_may_change(self, delegation):
        """Refuse a change to *delegation* by anybody but its delegator or their administrator.

        A delegation is its delegator's own. Its delegate is excluded on
        purpose, since extending one's own borrowed authority is exactly what
        must not be possible. An administrator holding template update or
        approvers assign may change it only for somebody whose approvals stay
        inside their branches
        (:func:`vs_rbac.grant_reach.assert_caller_may_change_person`).
        """
        from rest_framework.exceptions import PermissionDenied
        from vs_rbac.grant_reach import assert_caller_may_change_person

        user = self.request.user
        if delegation.delegator_id == user.pk:
            return
        if not self._is_delegation_admin():
            raise PermissionDenied(DELEGATION_NOT_YOURS)
        assert_caller_may_change_person(
            user, self.request.tenant, delegation.delegator, message=DELEGATION_SHARED,
        )

    def perform_update(self, serializer):
        self._assert_may_change(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        """Delete a delegation, taking its delegate off the requests still waiting on them first."""
        self._assert_may_change(instance)
        with transaction.atomic():
            if instance.revoked_at is None:
                instance.revoked_at = timezone.now()
                instance.save(update_fields=["revoked_at"])
                reassignment_svc.withdraw_delegation(instance, actor=self.request.user)
            instance.delete()

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        """POST - end a delegation now, and take its delegate off the requests still waiting on them.

        Where the delegate has already decided a request, that decision
        stands. Where an exclusive delegation had taken the delegator off, the
        delegator is put back.
        """
        delegation = self.get_object()
        self._assert_may_change(delegation)
        with transaction.atomic():
            # Revocation is timestamped instead of deleting the delegation record.
            delegation.revoked_at = timezone.now()
            delegation.save(update_fields=["revoked_at"])
            reassignment_svc.withdraw_delegation(delegation, actor=request.user)
        return Response(ApprovalDelegationSerializer(
            delegation, context=self.get_serializer_context(),
        ).data)
