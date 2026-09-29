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
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.permissions import IsAuthenticatedAndActive
from core.search import search as core_search

from ..constants import PERM_CREATE, PERM_UPDATE, PERM_VIEW, EmploymentStatus
from ..exceptions import FieldNotSelfEditable
from ..serializers import (
    StaffCreateSerializer,
    StaffDetailSerializer,
    StaffListSerializer,
    StaffUpdateSerializer,
)
from ..services import creation, hire, number_policy, posting, roles
from ..services.directory import counts
from ..services.scoping import guard_postings, is_self
from ..services.visibility import GROUP_CONTACT, PROFILE_FULL
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

#: The only roles a school may hand out before it goes live; see ``services.roles``.
ONBOARDING_ROLE_KEYS = roles.ONBOARDING_ROLE_KEYS


class StaffListCreateView(StaffViewMixin, generics.ListCreateAPIView):
    """GET/POST /v1/i/me/staff/ - this school's people, and adding one.

    Takes no tenant identifier: the school is ``request.tenant``'s, so there is
    nothing to tamper with and no way to list another school's people.

    Filters: ``search``, ``employment_status`` (including ``PENDING_APPROVAL``
    for hires awaiting approval and the derived ``ON_LEAVE``),
    ``account_status``, ``role``, ``branch``, ``teaching`` and
    ``missing_documents=true``, the people lacking a document type the school
    expects. The last needs a records key, as does the ``missing_documents``
    count beside the page, which is null without one.

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
        if params.get("missing_documents") == "true":
            lacking = self._lacking_documents()
            queryset = queryset.filter(lacking) if lacking is not None else queryset.none()
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
        response.data["counts"] = counts(
            queryset, self.tenant, by_branch=self.multi_branch,
            lacking=self._lacking_documents(),
        )
        invitable = list(self._invitable_roles().prefetch_related("additional_branches"))
        response.data["role_options"] = [
            {"value": role.key, "label": role.name, "branch_ids": role.branch_ids}
            for role in invitable
        ]
        response.data["starting_role"] = self._starting_role_option(invitable)
        response.data["multi_branch"] = self.multi_branch
        return response

    def _lacking_documents(self):
        """The missing-documents condition, for a reader who may read records.

        None where the school expects no document or the reader holds no
        records key: whether somebody's certificates are on file is part of the
        records the directory key alone does not open.
        """
        if not hasattr(self, "_lacking"):
            from vs_rbac.permissions import has_permission

            from ..constants import PERM_RECORDS_VIEW
            from ..services.directory import lacking_documents

            self._lacking = None
            if has_permission(self.request.user, PERM_RECORDS_VIEW, tenant=self.tenant):
                self._lacking = lacking_documents(self._directory_rules()[1])
        return self._lacking

    def _directory_rules(self):
        """The starting role key and the required documents, read once per request."""
        if not hasattr(self, "_rules_read"):
            from ..services.rules import directory_rules

            self._rules_read = directory_rules(self.tenant)
        return self._rules_read

    def _starting_role_option(self, invitable):
        """The role the Add form grants without asking, or ``None`` to ask.

        ``None`` while the school is onboarding, where the form asks which of
        the two administrator roles to give. At a live school it names the
        school's starting role (Settings, Staff), in the school's own words for
        it, read from the active roles already loaded for ``role_options``. A
        school that has retired that role still gets the answer, named from the
        catalogue where it can be, so the form stays the same and the create
        explains what to restore.
        """
        if self.onboarding:
            return None
        from vs_rbac.models import TenantRoleTemplate

        key = self._directory_rules()[0]
        role = next((r for r in invitable if r.key == key), None)
        if role is None:
            role = TenantRoleTemplate.objects.filter(tenant=self.tenant, key=key).first()
        return {"value": key, "label": role.name if role else key}

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
        where. What this adds is the staff record, the qualifications and the
        teaching duties the form carries, written in the same transaction, so a
        person whose third qualification is refused is not left existing with
        two. Documents are uploaded to the record afterwards, on its own
        endpoint.

        Three of the school's staff rules apply here. The staff number is
        checked against the rule for the new person's posting, and issued
        where the rule issues numbers and none was typed. The role is the
        school's starting role. And where the school approves each hire, the
        account and the record are written without an invitation, the record
        reads Awaiting approval, and it is submitted to the New staff approval
        ladder, whose decision sends the invitation or closes the hire. The
        response's ``awaiting_approval`` says which happened.
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
        staff_number = number_policy.settle_number(
            self.tenant, data.get("staff_number", ""), branch=branch,
        )
        held = hire.needs_approval(self.tenant)

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
        # Left at PENDING_APPROVAL only where the school approves each hire.
        if not held:
            UserCreationService.finalize_invitation(user=user, requested_by=request.user)
        profile = creation.create_profile(
            tenant=self.tenant, user=user, actor=request.user,
            employment_status=EmploymentStatus.PENDING_APPROVAL if held else None,
            staff_number=staff_number,
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
        if held:
            hire.submit(profile, actor=request.user)

        body = StaffDetailSerializer(profile, context=self.serializer_context()).data
        body["awaiting_approval"] = held
        return success_response(
            message=(
                "Added. Their invitation is waiting for the hire to be approved."
                if held else "Invitation sent."
            ),
            data=body,
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
        role = roles.starting_role(
            self.tenant, requested=data.get("role"), actor=self.request.user,
        )
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
    different key. A person may change the details the school lets staff edit
    about themselves (Settings, Staff) and nothing else, and some details are
    never theirs to change whatever the school chooses: editing your own hire
    date is editing your own tenure, and editing your own job title is a
    promotion the school did not give you. The person's own record carries
    ``self_editable_fields`` so the edit drawer knows which boxes to open.

    A staff number sent in a PATCH is checked against the staff-number rule
    for the person's posting (the new one, where the same PATCH moves them).
    Sending back the number already stored is not a change and is not checked,
    so a record numbered before a rule existed can still be saved.

    Reading is wider than changing. Anybody who works at the school opens
    anybody else's record there, at any branch, and reads as much of it as the
    school's profile policy gives their standing to that person
    (``services/visibility.py``); a reader whose ``school.teachers.view``
    reaches the person reads it as their role allows. ``profile_view`` says
    which: ``full`` for that reader and for the person themselves,
    ``restricted`` for a line manager or a colleague. ``visible_sections``
    names the profile's groups this reader may open, and every key outside
    them is absent rather than blanked.

    ``?as_at=YYYY-MM-DD`` answers with the record as it stood at the end of
    that day, plus an ``as_at`` block naming the day and when the history
    starts (``as_at.py``). The live record carries ``history_starts``. Both are
    for a full view only: a restricted reader asking for a past day is
    refused with 403.

    docstring-name: A staff record
    """

    pending_tenant_surface = True
    profile_group = GROUP_CONTACT

    @property
    def rbac_permission(self):
        method = (getattr(self.request, "method", "") or "").upper()
        return PERM_VIEW if method in ("GET", "HEAD", "OPTIONS") else PERM_UPDATE

    def get_permissions(self):
        """A person corrects their own record without holding the update key.

        The key check is skipped only for the caller's own row, and the fields
        they may send are narrowed in :meth:`patch`. A read is admitted by
        :meth:`admit_profile_read` for everybody.
        """
        from vs_rbac.permissions import IsAuthenticatedAndActive

        if not self._is_read() and self._is_own_record():
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

        staff, access, admission = self.admit_profile_read(pk)
        as_at = parse_as_at(request)
        self.refuse_as_at_unless_full(as_at, admission)
        context = {**self.serializer_context(), "profile_access": access}
        if as_at is None:
            data = StaffDetailSerializer(staff, context=context).data
            if access.profile_view == PROFILE_FULL:
                starts = past.staff_history_starts(staff.pk)
                data["history_starts"] = starts.isoformat() if starts else None
            return success_response(data=data)
        record, _children, meta = past.staff_at(staff, as_at)
        context = {**context, "as_at": as_at, "on_leave_ids": set()}
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
            from ..services.rules import self_editable_fields

            forbidden = sorted(set(data) - self_editable_fields(self.tenant))
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

        governing_branch = staff.branch
        if "branch" in data:
            branch = posting.resolve_posting(self.tenant, data.pop("branch"))
            guard_postings(request.user, self.tenant, [branch] if branch else [])
            posting.set_posting(staff, branch, actor=request.user)
            governing_branch = branch

        if "staff_number" in data:
            # An echo of the stored number is not a change and is not re-checked.
            data["staff_number"] = (data["staff_number"] or "").strip()
            if data["staff_number"].lower() == (staff.staff_number or "").lower():
                data.pop("staff_number")
            else:
                data["staff_number"] = number_policy.settle_number(
                    self.tenant, data["staff_number"], branch=governing_branch,
                    exclude_pk=staff.pk, issue=False,
                )

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


class StaffMineView(StaffViewMixin, APIView):
    """GET /v1/i/me/staff/mine/ - the caller's own staff record, by id.

    Every member of staff reaches their own record, and the way in is not the
    directory: a teacher holds no directory key, and their own record is where
    they apply for leave and correct their phone number. This answers only
    "which record is mine", with no key, so the app can link to it. 404 for an
    account at this school with no staff record.

    docstring-name: My staff record
    """

    permission_classes = [IsAuthenticatedAndActive]
    pending_tenant_surface = True

    def get(self, request):
        from ..models import StaffProfile

        row = (
            StaffProfile.all_objects.filter(tenant=self.tenant, user_id=request.user.pk)
            .values("id")
            .first()
        )
        if row is None:
            raise NotFound("You have no staff record at this school.")
        return success_response(data={"id": row["id"]})


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
