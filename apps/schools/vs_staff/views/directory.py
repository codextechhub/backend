"""The directory, the record, and the search behind the command palette.

The directory replaces what ``/v1/i/me/staff/`` did with a narrower queryset and
a wider payload: the people who have a staff record rather than every account
the tenant owns, and enough per row to draw the screen without a second call.

FRD M12 v2.1, FR-001, FR-002, FR-004 and FR-021.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_CREATE, PERM_UPDATE, PERM_VIEW, EmploymentStatus
from ..exceptions import FieldNotSelfEditable
from ..serializers import (
    SELF_EDITABLE_FIELDS,
    StaffCreateSerializer,
    StaffDetailSerializer,
    StaffListSerializer,
    StaffUpdateSerializer,
)
from ..services import creation, posting, roles
from ..services.directory import counts
from ..services.scoping import is_self
from .base import StaffViewMixin

#: The only roles a school may hand out before it goes live.
#:
#: Onboarding has one administrator in it, so there is nobody to review what
#: they grant. A bursar invited as Payout Approver during onboarding holds that
#: grant the moment the school goes live, and no second pair of eyes ever saw
#: it. Matched on the role KEY rather than the name, because the name is the
#: school's to rename and the key is not.
ONBOARDING_ROLE_KEYS = ("school_admin", "branch_admin")


class StaffListCreateView(StaffViewMixin, generics.ListCreateAPIView):
    """GET/POST /v1/i/me/staff/ - this school's people, and adding one.

    Takes no tenant identifier: the school is ``request.tenant``'s, so there is
    nothing to tamper with and no way to list another school's people.

    docstring-name: My school's staff
    """

    pending_tenant_surface = True

    @property
    def rbac_permission(self) -> str:
        method = (getattr(self.request, "method", "") or "").upper()
        return PERM_VIEW if method in ("GET", "HEAD", "OPTIONS") else PERM_CREATE

    def get_serializer_class(self):
        return (
            StaffCreateSerializer
            if self.request.method == "POST"
            else StaffListSerializer
        )

    def get_serializer_context(self):
        return self.serializer_context()

    def get_queryset(self):
        queryset = self.scoped_staff()
        params = self.request.query_params

        if search := (params.get("search") or "").strip():
            queryset = queryset.filter(
                Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search)
                | Q(user__email__icontains=search)
                | Q(staff_number__icontains=search)
                | Q(middle_name__icontains=search),
            )
        if value := params.get("employment_status"):
            # Filtered on what the row READS as, not on what the column holds,
            # or the facet would disagree with the chip beside every name.
            # Asking for On Leave asks whose leave is running; asking for
            # anything else excludes them, so the facets stay disjoint and the
            # header's figures still sum to the total.
            if value == EmploymentStatus.ON_LEAVE:
                queryset = queryset.filter(
                    employment_status=EmploymentStatus.ACTIVE, is_on_leave=True,
                )
            else:
                queryset = queryset.filter(employment_status=value).exclude(
                    employment_status=EmploymentStatus.ACTIVE, is_on_leave=True,
                )
        # A SEPARATE filter, never merged with the first. They answer different
        # questions and a single control would tell a school its locked-out
        # teacher had been suspended.
        if value := params.get("account_status"):
            queryset = queryset.filter(user__status=value)
        if value := params.get("role"):
            queryset = queryset.filter(
                user__tenant_role_assignments__role__key=value,
                user__tenant_role_assignments__assignment_status="ACTIVE",
            ).distinct()
        if (value := params.get("branch")) and self.multi_branch:
            if value == "school":
                queryset = queryset.filter(branch__isnull=True)
            else:
                branch = posting.resolve_posting(self.tenant, value)
                queryset = queryset.filter(branch=branch)
        if (value := params.get("teaching")) in ("true", "false"):
            wants = value == "true"
            queryset = (
                queryset.filter(teaching_load__gt=0)
                if wants
                else queryset.filter(teaching_load=0)
            )
        return queryset

    def list(self, request, *args, **kwargs):
        """The page, the counts, and the roles a new invitation may be given.

        All three together, because a directory that needs three calls to draw
        its header draws it late. The role options ship with the list for the
        same reason the school profile ships its choice vocabularies: a form
        that hard-codes role keys drifts from the roles this school actually
        has, and the school's roles endpoint is a separate surface it may not
        hold.
        """
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        response = self.get_paginated_response(serializer.data)
        response.data["counts"] = counts(queryset, self.tenant)
        response.data["role_options"] = [
            {"value": row["key"], "label": row["name"]}
            for row in self._invitable_roles().values("key", "name")
        ]
        response.data["multi_branch"] = self.multi_branch
        return response

    def _invitable_roles(self):
        """The roles this school may give out right now.

        One queryset behind both halves of the rule - the options the form
        offers and the check the create runs - so the dropdown can never offer a
        role the POST would refuse, and narrowing the dropdown can never be
        mistaken for enforcing anything.
        """
        from vs_rbac.models import TenantRoleTemplate

        queryset = TenantRoleTemplate.objects.filter(tenant=self.tenant, status="ACTIVE")
        if self.onboarding:
            queryset = queryset.filter(key__in=ONBOARDING_ROLE_KEYS)
        return queryset.order_by("name")

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """Add somebody, in one transaction over the form's six steps.

        The account, the invitation and the grant are made by
        ``UserCreationService``, which the live endpoint already called: a second
        creation path would be a second set of rules about who may be created
        where. What this adds is the staff record and the three child
        collections the design's form carries, written in the same transaction,
        so a person whose third qualification is refused is not left existing
        with two.
        """
        from vs_user.serializers import UserCreateSerializer
        from vs_user.services.user import UserCreationService

        payload = StaffCreateSerializer(
            data=request.data, context=self.serializer_context(),
        )
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        role = roles.resolve_role(
            self.tenant, data["role"],
            onboarding_keys=ONBOARDING_ROLE_KEYS if self.onboarding else None,
        )
        branch = posting.resolve_posting(self.tenant, data.get("branch"))
        role_branch = posting.resolve_posting(self.tenant, data.get("role_branch"))

        account = UserCreateSerializer(
            data={
                "first_name": data["first_name"],
                "last_name": data["last_name"],
                "email": data["email"],
                "phone": data.get("phone", ""),
                "gender": data.get("gender", ""),
                "role": role.key,
                "branch": str(branch.pk) if branch else None,
            },
            context={"request": request},
        )
        account.is_valid(raise_exception=True)

        # The serializer resolves the owning tenant from the ACTOR: a CodeX
        # platform user calling here resolves to the codex tenant, not to the
        # school in ?tenant=. Left alone, a CodeX staffer hitting a school
        # endpoint would create a colleague on the platform and skip the
        # approval workflow the platform endpoint puts in front of that.
        if account.validated_data.get("tenant") != self.tenant:
            raise PermissionDenied(
                "Staff can only be added to the school you are signed in to.",
            )

        user = UserCreationService.create_pending(
            account.validated_data, request.user, request=request,
        )
        # ``create_pending`` defaults to PENDING_APPROVAL, which is the platform
        # hiring workflow's state and not a school's: a school approves nobody,
        # so an account parked there would be an invitation that never goes out
        # and a person nobody can chase. ``finalize_invitation`` moves it to
        # PENDING and dispatches the email, which is the state FR-001 promises.
        UserCreationService.finalize_invitation(user=user, requested_by=request.user)
        profile = creation.create_profile(
            tenant=self.tenant, user=user, actor=request.user,
            staff_number=data.get("staff_number", ""),
            job_title=data.get("job_title", ""),
            employment_type=data.get("employment_type", ""),
            hire_date=data.get("hire_date"), branch=branch,
            middle_name=data.get("middle_name", ""),
            date_of_birth=data.get("date_of_birth"), photo=data.get("photo"),
        )
        creation.attach_qualifications(
            profile, data.get("qualifications"), actor=request.user,
        )
        if role_branch is not None:
            self._pin_grant_to_branch(user, role, role_branch, request.user)
        self._attach_teaching(profile, data, request.user)

        return success_response(
            message="Invitation sent.",
            data=StaffDetailSerializer(
                profile, context=self.serializer_context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )

    def _pin_grant_to_branch(self, user, role, branch, actor):
        """Move the grant the creation service made onto one branch.

        The service grants school-wide, which is right for a registrar and wrong
        for a teacher hired at one site. Rewriting the row rather than adding a
        second one, because a whole-tenant grant dominates and a pinned one
        beside it would confer nothing.
        """
        from vs_rbac.models import TenantUserRoleAssignment

        grant = TenantUserRoleAssignment.objects.filter(
            tenant=self.tenant, user=user, role=role,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        ).first()
        if grant is None:
            TenantUserRoleAssignment.objects.create(
                tenant=self.tenant, user=user, role=role, branch=branch,
                assigned_by=actor,
            )
            return
        if grant.branch_id != branch.pk:
            grant.branch = branch
            grant.save(update_fields=["branch", "updated_at"])

    def _attach_teaching(self, profile, data, actor):
        """The form's subjects-by-classes grid, if it carried one.

        Silently skipped where the school has no active session: teaching
        duties belong to a year, and a school still onboarding has not started
        one. The screen offers the step only where it applies.
        """
        subject_ids = data.get("subjects") or []
        class_ids = data.get("classes") or []
        if not subject_ids or not class_ids or self.active_session is None:
            return
        from schools.vs_academics.models import SchoolClass, Subject

        subjects = list(Subject.objects.filter(tenant=self.tenant, pk__in=subject_ids))
        classes = list(
            SchoolClass.objects.filter(
                tenant=self.tenant, pk__in=class_ids, session=self.active_session,
            ),
        )
        creation.attach_teaching(
            profile, subjects=subjects, classes=classes,
            session=self.active_session, actor=actor,
        )


class StaffDetailView(StaffViewMixin, APIView):
    """GET/PATCH /v1/i/me/staff/<id>/ - one person's record.

    Two audiences and two payloads. An administrator holding
    ``school.teachers.update`` may change everything here except the employment
    status, which moves only through the lifecycle, and the email, which is a
    different key. A person may change a whitelist of personal facts about
    themselves and nothing else: editing your own hire date is editing your own
    tenure, and editing your own job title is a promotion the school did not
    give you.

    docstring-name: A staff record
    """

    pending_tenant_surface = True

    @property
    def rbac_permission(self):
        method = (getattr(self.request, "method", "") or "").upper()
        return PERM_VIEW if method in ("GET", "HEAD", "OPTIONS") else PERM_UPDATE

    def get_permissions(self):
        """A person reaches their own record without holding anything.

        The key check is skipped only for the caller's own row, and every
        queryset below is still scoped, so this widens who may read one record
        rather than what any of them may read.
        """
        from vs_rbac.permissions import IsAuthenticatedAndActive

        if self._is_own_record():
            return [IsAuthenticatedAndActive()]
        return super().get_permissions()

    def _is_own_record(self) -> bool:
        from ..models import StaffProfile

        pk = self.kwargs.get("pk")
        user = getattr(self.request, "user", None)
        tenant = getattr(self.request, "tenant", None)
        if not getattr(user, "pk", None) or tenant is None:
            return False
        return StaffProfile.objects.filter(
            tenant=tenant, pk=pk, user_id=user.pk,
        ).exists()

    def get(self, request, pk):
        staff = self.get_staff(pk)
        return success_response(
            data=StaffDetailSerializer(staff, context=self.serializer_context()).data,
        )

    @transaction.atomic
    def patch(self, request, pk):
        staff = self.get_staff(pk)
        payload = StaffUpdateSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)

        # The self-restriction applies to somebody editing their own record
        # WITHOUT administrative authority over records generally. A caller
        # holding school.teachers.update can already change every colleague's
        # hire date, so refusing them their own changes nothing about what they
        # can do and does strand the school whose only administrator needs to
        # correct her own job title. FRD v2.1 says "at any permission level";
        # that reading makes a single-administrator school unable to fix its own
        # record, and the document is the half that moves.
        if is_self(request.user, staff) and not self._holds_update_key():
            forbidden = sorted(set(data) - SELF_EDITABLE_FIELDS)
            if forbidden:
                # 422 with the fields named rather than a bare 403, so the form
                # can show it where the reader has to change it.
                raise FieldNotSelfEditable(
                    "You cannot change "
                    + ", ".join(field.replace("_", " ") for field in forbidden)
                    + " about yourself. Ask a school administrator.",
                    fields=forbidden,
                )

        user_fields = {}
        for field in ("phone", "gender", "first_name", "last_name"):
            if field in data:
                user_fields[field] = data.pop(field)

        if "branch" in data:
            branch = posting.resolve_posting(self.tenant, data.pop("branch"))
            posting.set_posting(staff, branch, actor=request.user)

        if "staff_number" in data:
            data["staff_number"] = (data["staff_number"] or "").strip()

        for field, value in data.items():
            setattr(staff, field, value)
        if data:
            staff.full_clean(exclude=["photo"])
            staff.save()

        if user_fields:
            for field, value in user_fields.items():
                setattr(staff.user, field, value)
            staff.user.save(update_fields=[*user_fields, "updated_at"])

        staff.refresh_from_db()
        return success_response(
            message="Record updated.",
            data=StaffDetailSerializer(
                self.get_staff(pk), context=self.serializer_context(),
            ).data,
        )

    def _holds_update_key(self) -> bool:
        from vs_rbac.permissions import has_permission

        return has_permission(self.request.user, PERM_UPDATE, tenant=self.tenant)


class StaffSearchView(StaffViewMixin, APIView):
    """GET /v1/i/me/staff/search/?q= - the command palette's staff hits.

    Reads the same three fields the directory's ``?search=`` reads, through the
    same scoping, so the palette can never find somebody the directory cannot
    open. Capped small, because it renders in a dropdown and a hundred rows is a
    scroll nobody reads, and it carries no email address: this is the most
    casually visible surface in the module.

    docstring-name: Search this school's staff
    """

    rbac_permission = PERM_VIEW
    pending_tenant_surface = True

    #: Below this, a search is a keystroke rather than a query, and returning
    #: the whole school for "a" is how a dropdown becomes a roster.
    MIN_QUERY = 2
    LIMIT = 10

    def get(self, request):
        query = (request.query_params.get("q") or "").strip()
        if len(query) < self.MIN_QUERY:
            return success_response(data=[])

        rows = self.scoped_staff().filter(
            Q(user__first_name__icontains=query)
            | Q(user__last_name__icontains=query)
            | Q(user__email__icontains=query)
            | Q(staff_number__icontains=query),
        )[: self.LIMIT]

        return success_response(data=[
            {
                "id": row.pk,
                "name": " ".join(
                    part for part in (row.user.first_name, row.user.last_name) if part
                ).strip(),
                "meta": row.job_title or row.staff_number or "",
                "employment_status": row.employment_status,
            }
            for row in rows
        ])
