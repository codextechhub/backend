"""The school's own staff list, and inviting somebody onto it.

Why this exists beside ``vs_user``'s account endpoints rather than opening them:
those are gated on ``platform.team.*``, which a school administrator does not
hold and should not. A school runs its own staff and holds
``school.administrators.*`` for exactly that, so the surface it needs is a
school-scoped one - the same shape, and the same reasoning, as
``/v1/i/me/profile/``.

Three things are deliberate.

**It takes no tenant identifier.** The school is ``request.tenant``'s, so there
is no id to tamper with and no way to list another school's people.

**It is on the pending-tenant surface.** "Add Staff & Invitations" is a step on
the school's own checklist, so it has to work before go-live or the step is one
a school can be asked for and never do.

**It reuses ``UserCreateSerializer`` and ``UserCreationService``.** Between them
they already resolve the target tenant from ``request.tenant`` for a non-
platform actor, resolve the role inside that tenant, scope the email-uniqueness
check to it, and send the invitation. Writing a second creation path would be a
second set of rules about who may be created where, and the two would drift.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import error_response, success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_tenants.models import Tenant

from ..serializers import SchoolStaffSerializer

User = get_user_model()


class _SchoolStaffBase:
    """Shared wiring: this school, this school's keys, open before go-live."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    pending_tenant_surface = True

    #: The only roles a school may hand out before it goes live.
    #:
    #: Onboarding has one administrator in it, so there is nobody to review
    #: what she grants. A bursar invited as Payout Approver during onboarding
    #: holds that grant the moment the school goes live, and no second pair of
    #: eyes ever saw it. Admin roles are the two a school genuinely needs to
    #: finish onboarding; everything else waits for a live system, where role
    #: assignment is reviewable.
    #:
    #: Matched on the role KEY rather than the name, because the name is the
    #: school's to rename and the key is not.
    ONBOARDING_ROLE_KEYS = ("school_admin", "branch_admin")

    @property
    def tenant(self):
        return getattr(self.request, "tenant", None)

    @property
    def onboarding(self) -> bool:
        """True while this school has not gone live."""
        return getattr(self.tenant, "status", None) == Tenant.Status.PENDING

    def invitable_roles(self):
        """The roles this school may invite somebody into right now.

        One queryset behind both halves of the rule - the options the form
        offers and the check the create runs - so the dropdown can never offer
        a role the POST would refuse, and narrowing the dropdown can never be
        mistaken for enforcing anything.
        """
        from vs_rbac.models import TenantRoleTemplate

        roles = TenantRoleTemplate.objects.filter(
            tenant=self.tenant, status="ACTIVE",
        )
        if self.onboarding:
            roles = roles.filter(key__in=self.ONBOARDING_ROLE_KEYS)
        return roles.order_by("name")

    #: Half-made and refused accounts, which are not invitations.
    #:
    #: A DRAFT is a record parked before anybody was invited, and REJECTED is a
    #: creation that was turned down. Neither is a person waiting on an email,
    #: so listing them under "Invitations sent" would tell a school it had
    #: chased somebody it never wrote to. Same exclusion the platform's own
    #: accounts list applies, for the same reason.
    HIDDEN_STATUSES = (
        User.Status.DRAFT,
        User.Status.PENDING_APPROVAL,
        User.Status.REJECTED,
    )

    def school_users(self):
        """Everyone in this school, newest first.

        ``select_related`` on the invitation because the list renders each
        person's invitation state, and ``prefetch_related`` on the role
        assignments because it renders their role: without either, a page of
        25 people is 50 extra queries.

        A note for whoever adds students and parents: this is every USER the
        tenant owns, which today is only its staff. If those ever become user
        rows on the same tenant, this filter has to narrow, or a school's staff
        list becomes its whole roll.
        """
        return (
            User.objects.filter(tenant=self.tenant)
            .exclude(status__in=self.HIDDEN_STATUSES)
            .select_related("invitation", "branch")
            .prefetch_related(
                "tenant_role_assignments__role",
            )
            .order_by("-created_at", "-id")
        )


class SchoolStaffListCreateView(_SchoolStaffBase, generics.ListCreateAPIView):
    """GET/POST /v1/i/me/staff/ - this school's people, and inviting one.

    docstring-name: My school's staff
    """

    pagination_class = XVSPagination

    @property
    def rbac_permission(self) -> str:
        method = (getattr(self.request, "method", "") or "").upper()
        return (
            "school.administrators.view"
            if method in ("GET", "HEAD", "OPTIONS")
            else "school.administrators.create"
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            from vs_user.serializers import UserCreateSerializer

            return UserCreateSerializer
        return SchoolStaffSerializer

    def get_queryset(self):
        return self.school_users()

    def list(self, request, *args, **kwargs):
        """The staff list, plus the roles a new invitation may be given.

        The role options ship with the list for the same reason the school
        profile ships its choice vocabularies: an invite form that hard-codes
        role keys drifts from the roles this school actually has, and the
        school's roles endpoint is a separate surface it may not hold.
        """
        page = self.paginate_queryset(self.get_queryset())
        serializer = self.get_serializer(page, many=True)
        roles = self.invitable_roles().values("key", "name")
        response = self.get_paginated_response(serializer.data)
        response.data["role_options"] = [
            {"value": row["key"], "label": row["name"]} for row in roles
        ]
        return response

    def create(self, request, *args, **kwargs):
        """Invite somebody onto this school's staff.

        ``UserCreateSerializer`` validates but does not save - creation lives in
        ``UserCreationService``, which is what the platform's own account
        endpoint calls. The two calls are the school half of that endpoint's
        create: make the pending account, then send the invitation. The
        workflow branch it has is deliberately not here, because that branch is
        for provisioning CodeX platform staff and this surface never does.
        """
        from vs_user.services.user import UserCreationService

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # The serializer resolves the owning tenant from the ACTOR: a CodeX
        # platform user calling here resolves to the codex tenant, not to the
        # school in ?tenant=. Left alone, a Codex staffer hitting a school
        # endpoint would create a colleague on the platform and skip the
        # approval workflow the platform endpoint puts in front of that. This
        # surface only ever creates people in the school it is scoped to.
        if serializer.validated_data.get("tenant") != self.tenant:
            return error_response(
                message="Staff can only be added to the school you are signed in to.",
                status=status.HTTP_403_FORBIDDEN,
            )

        # The same rule the dropdown was narrowed by, asked again here - and
        # asked of the same queryset, not of a parallel list of keys, so the
        # two can never disagree. A narrowed dropdown is a courtesy; this is
        # the rule. Without it a crafted request assigns Payout Approver to a
        # bursar during onboarding and the courtesy has changed nothing.
        #
        # It runs for a live school too, where it is not a narrowing but a
        # sanity check: the role must still be one this school actually has,
        # and active.
        role = serializer.validated_data.get("role_instance")
        if role is None or not self.invitable_roles().filter(pk=role.pk).exists():
            # Raised rather than returned, so it lands in the same envelope as
            # every other field refusal ({error: {code, detail: {role: [...]}}})
            # and the form shows it under the field the reader has to change.
            raise ValidationError({"role": [
                "Only administrators can be invited while your school is "
                "onboarding. Other roles can be assigned once you go live."
                if self.onboarding
                else "That is not a role your school can assign."
            ]})

        with transaction.atomic():
            user = UserCreationService.create_pending(
                validated_data=serializer.validated_data,
                requesting_user=request.user,
                request=request,
            )
            UserCreationService.finalize_invitation(
                user=user, requested_by=request.user,
            )

        return success_response(
            message="Invitation sent.",
            data=SchoolStaffSerializer(
                user, context=self.get_serializer_context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )


class SchoolStaffResendView(_SchoolStaffBase, APIView):
    """POST /v1/i/me/staff/<id>/resend/ - send the invitation again.

    Scoped to this school before anything else happens: another school's user
    answers 404, never 403, so ids cannot be probed for existence.

    docstring-name: Resend a staff invitation
    """

    rbac_permission = "school.administrators.create"

    def post(self, request, pk: int):
        from vs_user.services.invitation import InvitationService

        user = User.objects.filter(pk=pk, tenant=self.tenant).first()
        if user is None:
            raise NotFound("No such person at this school.")

        if user.status != User.Status.PENDING:
            return error_response(
                message=(
                    "Invitations can only be resent for accounts that have not "
                    "been activated yet."
                ),
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        try:
            InvitationService.resend(
                user=user, requested_by=request.user, request=request,
            )
        except Exception as error:  # noqa: BLE001 - the service raises loosely
            payload = error.args[0] if error.args else {}
            message = (
                payload.get("detail", "We could not resend that invitation.")
                if isinstance(payload, dict)
                else str(payload)
            )
            return error_response(message=message, error=payload)

        return success_response(
            message="Invitation sent again.",
            data=SchoolStaffSerializer(
                user, context=self.get_serializer_context()
            ).data,
        )

    def get_serializer_context(self):
        return {"request": self.request}
