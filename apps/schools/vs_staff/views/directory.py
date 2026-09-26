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
from core.search import search as core_search

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
from ..services.scoping import guard_postings, is_self
from .base import StaffViewMixin

#: What a typed query is matched against, joined into one string.
#:
#: The staff number is in here with the names on purpose: a school that files by
#: number types a number, and a box that only searched names would answer
#: nothing for the half of a school that does.
SEARCH_FIELDS = (
    "user__first_name", "middle_name", "user__last_name", "user__email",
    "staff_number",
)

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

        # Matched loosely and RANKED, not field by field against the whole
        # string. The old shape searched each column for the entire query, so
        # the thing a reader tries first - typing somebody's full name -
        # returned nothing, because "Sunday Ekpo" sits in no single field.
        queryset = core_search(
            queryset, params.get("search"),
            fields=SEARCH_FIELDS, then=("-created_at", "-id"),
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
            # Locked is not a stored status. It is a lockout that expires on its
            # own, so both halves of this filter ask the lockout row: Locked
            # finds whoever is locked at this moment, and every other value
            # excludes them, so the facets stay disjoint the way the employment
            # ones above do and the counts beside them still sum.
            from vs_user.models import User, lockout_in_force

            if value == User.Status.LOCKED:
                queryset = queryset.filter(lockout_in_force("user__"))
            else:
                queryset = queryset.filter(user__status=value).exclude(
                    lockout_in_force("user__"),
                )
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
        response.data["counts"] = counts(queryset, self.tenant, by_branch=self.multi_branch)
        invitable = list(self._invitable_roles().prefetch_related("additional_branches"))
        response.data["role_options"] = [
            {"value": role.key, "label": role.name, "branch_ids": role.branch_ids}
            for role in invitable
        ]
        response.data["starting_role"] = self._starting_role_option(invitable)
        response.data["multi_branch"] = self.multi_branch
        return response

    def _starting_role_option(self, invitable):
        """The role the Add form grants without asking, or ``None`` to ask.

        ``None`` while the school is onboarding, where the form asks which of
        the two administrator roles to give. At a live school it names the
        starting role, in the school's own words for it, read from the active
        roles already loaded for ``role_options``. A school that has retired
        that role still gets the answer, so the form stays the same and the
        create explains what to restore.
        """
        if self.onboarding:
            return None
        role = next((r for r in invitable if r.key == roles.STARTING_ROLE_KEY), None)
        return {
            "value": roles.STARTING_ROLE_KEY,
            "label": role.name if role else "Teacher",
        }

    def _invitable_roles(self):
        """The roles this school may give out right now.

        The options the role drawers offer, and during onboarding the options
        the invitation offers. ``roles.resolve_role`` applies the same
        onboarding narrowing to every grant, so the dropdown never offers a
        role the POST would refuse, and narrowing it is never mistaken for
        enforcing anything.
        """
        from vs_rbac.models import TenantRoleTemplate

        queryset = TenantRoleTemplate.objects.filter(tenant=self.tenant, status="ACTIVE")
        if self.onboarding:
            queryset = queryset.filter(key__in=ONBOARDING_ROLE_KEYS)
        return queryset.order_by("name")

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """Add somebody, in one transaction over the form's steps.

        The role is the server's to choose at a live school: see
        :meth:`_grant_for`.

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

        role, reach = self._grant_for(data)
        # A branch administrator's new person is filed under their branch.
        requested = posting.resolve_posting(self.tenant, data.get("branch"))
        branch = next(iter(guard_postings(
            request.user, self.tenant, [requested] if requested else [],
            default_when_unset=True,
        )), None)

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
            role_branch=reach,
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
        self._attach_teaching(profile, data, request.user)

        return success_response(
            message="Invitation sent.",
            data=StaffDetailSerializer(
                profile, context=self.serializer_context(),
            ).data,
            status=status.HTTP_201_CREATED,
        )

    def _grant_for(self, data):
        """The role a new person is given, and how far it reaches.

        While the school is onboarding the adder picks School Admin or Branch
        Admin, and may say how far it reaches, because onboarding is how a
        school's first administrators arrive. Once it is live the adder picks
        neither: everybody starts on the starting role, reaching as far as
        their posting, and a role admin widens, narrows or replaces it later.
        Both are refused rather than ignored when sent, so a caller never
        believes it granted something it did not.
        """
        if self.onboarding:
            if not data.get("role"):
                raise ValidationError({
                    "role": "Choose School Admin or Branch Admin for them.",
                })
            role = roles.resolve_role(
                self.tenant, data["role"], onboarding_keys=ONBOARDING_ROLE_KEYS,
            )
            return role, posting.resolve_reach(self.tenant, data.get("role_branch"))

        if data.get("role_branch"):
            raise ValidationError({
                "role_branch": (
                    "A new member of staff's role reaches as far as their "
                    "posting. Change it from Roles & Permissions once they are "
                    "added."
                ),
            })
        role = roles.starting_role(self.tenant, requested=data.get("role"))
        return role, posting.resolve_reach(self.tenant, None)

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

    ``?as_at=YYYY-MM-DD`` answers with the record as it stood at the end of
    that day, plus an ``as_at`` block naming the day and when the history
    starts (``as_at.py``). The live record carries ``history_starts``.

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
        from vs_history.as_at import parse_as_at

        from .. import as_at as past

        staff = self.get_staff(pk)
        as_at = parse_as_at(request)
        if as_at is None:
            data = StaffDetailSerializer(staff, context=self.serializer_context()).data
            starts = past.staff_history_starts(staff.pk)
            data["history_starts"] = starts.isoformat() if starts else None
            return success_response(data=data)
        record, _children, meta = past.staff_at(staff, as_at)
        context = {**self.serializer_context(), "as_at": as_at, "on_leave_ids": set()}
        data = StaffDetailSerializer(record, context=context).data
        data["history_starts"] = meta["history_starts"]
        data["as_at"] = meta
        return success_response(data=data)

    @transaction.atomic
    def patch(self, request, pk):
        staff = self.get_staff_for_write(pk)
        # The record and the request both ride in, or the field guard has
        # neither a caller to judge nor a stored value to recognise an echo by.
        payload = StaffUpdateSerializer(
            staff, data=request.data, partial=True,
            context=self.serializer_context(),
        )
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
            guard_postings(request.user, self.tenant, [branch] if branch else [])
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

    Reads the same fields through the same matcher and the same scoping, so the
    palette can never find somebody the directory cannot open, and never miss
    somebody it would have found. Capped small, because it renders in a dropdown and a hundred rows is a
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

        # The same matcher the directory uses, so the palette can never find
        # somebody the directory cannot, and never fail to find somebody it can.
        rows = core_search(
            self.scoped_staff(), query,
            fields=SEARCH_FIELDS, then=("user__first_name", "user__last_name"),
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
