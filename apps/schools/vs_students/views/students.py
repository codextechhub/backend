"""The directory, the profile, enrolment and editing."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Prefetch, Q
from rest_framework import generics
from rest_framework.views import APIView

from core.response import success_response
from vs_audit.models import AuditActionType
from vs_audit.services import emit_audit_event
from vs_audit.models import AuditModuleKey

from ..constants import (
    DEFAULT_LIST_STATUSES,
    ON_ROLL,
    PERM_CLASS_ASSIGN,
    PERM_CREATE,
    PERM_UPDATE,
    PERM_VIEW,
    SEARCH_LIMIT,
    SEARCH_MIN_CHARS,
    StudentStatus,
)
from ..models import ClassEnrolment, Student, StudentGuardian
from ..serializers import (
    EnrolmentWriteSerializer,
    SearchHitSerializer,
    StudentDetailSerializer,
    StudentListSerializer,
    StudentWriteSerializer,
)
from ..services import enrolment as enrolment_service
from ..services.placement import fullest_classes, resolve_class
from ..services.scoping import branch_for_write, scope_students, UNSET
from .base import StudentsViewMixin


def _list_queryset(tenant, session=None):
    """One queryset with everything a row needs already prefetched.

    The prefetches are the whole reason a page of fifty students is a fixed
    number of queries rather than a hundred and fifty: without them each row
    fetches its own class and its own guardians, and the cost grows with the
    page size.

    **``session`` changes which enrolment the row reads, and must not filter on
    ``is_active``.** That flag marks a student's CURRENT placement, not the fact
    of having been on a roll: promoting Amaka out of 2026/2027 left her SSS1 A
    row with ``is_active=False``, so filtering on it answers "nobody was in
    SSS1 A last year". Ordering by it instead puts the year's live row first
    and keeps the superseded ones behind it, which is also what a mid-year
    transfer needs - two rows in one year, the later one current.
    """
    enrolments = ClassEnrolment.objects.select_related(
        "school_class", "school_class__level", "session",
    )
    enrolments = (
        enrolments.filter(session=session).order_by("-is_active", "-id")
        if session is not None
        else enrolments.filter(is_active=True)
    )
    active = Prefetch("enrolments", queryset=enrolments, to_attr="_active_enrolments")
    guardians = Prefetch(
        "guardian_links",
        queryset=StudentGuardian.objects.filter(is_primary=True).select_related(
            "guardian",
        ),
    )
    qs = Student.objects.filter(tenant=tenant)
    if session is not None:
        # The roll AS IT WAS: everyone with a placement that year, and nobody
        # who only exists in another one.
        qs = qs.filter(enrolments__session=session)
    return (
        qs.select_related("branch", "applied_for")
        .prefetch_related(active, guardians)
        .distinct()
    )


class StudentListCreateView(StudentsViewMixin, generics.ListCreateAPIView):
    """GET, POST /v1/students/

    docstring-name: Students
    """

    serializer_class = StudentListSerializer

    def get_permissions(self):
        # PERM_CLASS_ASSIGN is checked separately in create(), not listed
        # here: rbac_permission is any-of, so naming both would let a caller
        # holding either one alone enrol a student. See base.assert_holds.
        self.rbac_permission = (
            PERM_CREATE if self.request.method == "POST" else PERM_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        params = self.request.query_params
        qs = scope_students(
            _list_queryset(self.tenant, self.session_filter),
            self.request.user, self.tenant,
        )

        status = (params.get("status") or "").strip().upper()
        if status and status != "ALL":
            qs = qs.filter(status=status)
        elif not status:
            # A withdrawn or graduated student is still a record and still
            # findable by name; they are simply not what "the students at this
            # school" means on the screen that says so.
            qs = qs.filter(status__in=DEFAULT_LIST_STATUSES)

        search = (params.get("search") or "").strip()
        if search:
            # Must tolerate a student with no number rather than excluding
            # them, which an inner join on the number would do.
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(middle_name__icontains=search)
                | Q(student_number__icontains=search),
            )

        klass = (params.get("class") or "").strip()
        if klass:
            if klass.lower() in ("none", "unassigned"):
                # Not the same query as /unplaced/: the screen's "unassigned"
                # means on the roll with no class, not "no active enrolment",
                # which would sweep in applicants and leavers.
                qs = qs.filter(status__in=ON_ROLL).exclude(
                    enrolments__is_active=True,
                )
            else:
                qs = qs.filter(
                    enrolments__is_active=True, enrolments__school_class_id=klass,
                )

        level = (params.get("level") or "").strip()
        if level:
            qs = qs.filter(
                enrolments__is_active=True,
                enrolments__school_class__level_id=level,
            )

        return self.narrow_to_branch(qs).distinct()

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        # Two keys, not one. Enrolment creates a record AND seats the child,
        # and seating is vs_academics' power. Checked before validation so
        # the refusal is a 403 and not a validation error.
        self.assert_holds(PERM_CREATE, PERM_CLASS_ASSIGN)

        writer = EnrolmentWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        guardian_rows = data.pop("guardians")
        as_applicant = data.pop("as_applicant")

        branch = branch_for_write(
            request.user, self.tenant,
            data.pop("branch", UNSET) or UNSET,
        )

        school_class = None
        class_id = data.pop("school_class", None)
        if class_id and not as_applicant:
            school_class = resolve_class(self.tenant, request.user, class_id)

        level_id = data.pop("applied_for", None)
        if level_id:
            data["applied_for"] = self._level(level_id)

        for row in guardian_rows:
            gid = row.pop("guardian_id", None)
            row["guardian"] = self.guardian(gid) if gid else None

        student = enrolment_service.enrol(
            tenant=self.tenant, actor=request.user, branch=branch, data=data,
            guardian_rows=guardian_rows, as_applicant=as_applicant,
            school_class=school_class,
            allow_over_capacity=data.pop("allow_over_capacity", False),
            confirm_duplicate=data.pop("confirm_duplicate", False),
        )
        message = (
            f"{student.full_name} saved as an applicant." if as_applicant
            else f"{student.full_name} enrolled"
            + (f" as {student.student_number}" if student.student_number else "")
            + (f" in {school_class.name}." if school_class else ".")
        )
        return success_response(
            message,
            data=StudentDetailSerializer(
                student, context=self.get_serializer_context(),
            ).data,
            status=201,
        )

    def _level(self, pk):
        from rest_framework.exceptions import NotFound
        from schools.vs_academics.models import Level

        row = Level.objects.filter(tenant=self.tenant, pk=pk).first()
        if row is None:
            raise NotFound("No such level at this school.")
        return row


class StudentDetailView(StudentsViewMixin, generics.RetrieveUpdateAPIView):
    """GET, PATCH /v1/students/<id>/

    docstring-name: One student
    """

    serializer_class = StudentDetailSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        self.rbac_permission = (
            PERM_UPDATE if self.request.method == "PATCH" else PERM_VIEW
        )
        return super().get_permissions()

    def get_object(self):
        return self.student(self.kwargs["pk"])

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            "Student retrieved.",
            data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def patch(self, request, *args, **kwargs):
        student = self.get_object()
        before = StudentDetailSerializer(
            student, context=self.get_serializer_context(),
        ).data
        writer = StudentWriteSerializer(
            student, data=request.data, partial=True,
            context=self.get_serializer_context(),
        )
        writer.is_valid(raise_exception=True)

        number = writer.validated_data.get("student_number")
        if number is not None:
            from ..services.policy import assert_number_allowed

            value = assert_number_allowed(self.tenant, number)
            enrolment_service.assert_number_free(
                self.tenant, value, exclude_pk=student.pk,
            )
            writer.validated_data["student_number"] = value

        updated = writer.save()
        after = StudentDetailSerializer(
            updated, context=self.get_serializer_context(),
        ).data
        changed = {
            k: {"from": before.get(k), "to": after.get(k)}
            for k in after
            if before.get(k) != after.get(k)
        }
        emit_audit_event(
            module_key=AuditModuleKey.STUDENT, action_type=AuditActionType.UPDATE,
            entity_type="Student", entity_id=str(updated.pk),
            entity_label=updated.full_name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{updated.full_name}'s record updated.",
            before_data=before, diff_data=changed,
        )
        return success_response(
            f"{updated.full_name}'s record updated.", data=after,
        )


class UnplacedStudentsView(StudentsViewMixin, generics.ListAPIView):
    """GET /v1/students/unplaced/

    Students on the roll with no class. Deliberately narrower than "no active
    enrolment", which would also return applicants, leavers and graduates -
    and the count drives a badge in the navigation, so a wrong definition is a
    wrong number in front of the registrar all day.

    **This one does NOT take ``?session=``, and that is the answer rather than
    an omission.** It is a worklist: every row is a child somebody is being
    asked to place, and placing happens in the year the school is running.
    Asking it about 2026/2027 would return everyone who has since left - none
    of whom can be placed, because the module refuses writes against a closed
    year - so the list would fill with work nobody can do. The nav badge reads
    from here, and a badge counting impossible work is worse than no badge.

    docstring-name: Students with no class
    """

    serializer_class = StudentListSerializer

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get_queryset(self):
        # Narrowed like the directory it feeds: this count is a badge in the
        # navigation, and a whole-school figure beside a branch's roll sends
        # somebody looking for children who are not theirs to place.
        return self.narrow_to_branch(
            scope_students(
                _list_queryset(self.tenant).filter(status__in=ON_ROLL).exclude(
                    enrolments__is_active=True,
                ),
                self.request.user, self.tenant,
            ),
        ).distinct()


class StudentSearchView(StudentsViewMixin, APIView):
    """GET /v1/students/search/?q=

    The command palette. Capped and never paginated: a palette that paginates
    is a list, and this is not one.

    docstring-name: Search students
    """

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get(self, request):
        query = (request.query_params.get("q") or "").strip()
        if len(query) < SEARCH_MIN_CHARS:
            # Deliberately empty rather than the whole roll: a one-character
            # query is a keystroke, not a search.
            return success_response(data=[])
        rows = scope_students(
            _list_queryset(self.tenant).filter(
                Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(student_number__icontains=query),
            ),
            request.user, self.tenant,
        ).distinct()[:SEARCH_LIMIT]
        return success_response(data=SearchHitSerializer(rows, many=True).data)


class StudentSummaryView(StudentsViewMixin, APIView):
    """GET /v1/students/summary/

    The directory header. Aggregates over the same scoped queryset the list
    uses, so the figures can never describe more students than the screen
    below them shows.

    docstring-name: Student summary
    """

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get(self, request):
        # The same roll the table below shows, on both lenses. The figures
        # describing a different set of students from the rows underneath them
        # is the defect this endpoint has already been fixed for once.
        base = Student.objects.filter(tenant=self.tenant)
        if self.session_filter is not None:
            base = base.filter(enrolments__session=self.session_filter)
        scoped = self.narrow_to_branch(
            scope_students(base, request.user, self.tenant),
        )
        # distinct=True on the COUNT, not on the queryset: the session lens
        # joins through enrolments, and a student with two rows in a year - a
        # mid-year transfer - would otherwise be counted twice. A bare
        # .distinct() does not survive values().annotate(), which silently
        # collapsed the groups instead and reported 6 students out of 83.
        by_status = {
            row["status"]: row["n"]
            for row in scoped.values("status").annotate(n=Count("id", distinct=True))
        }
        unassigned = scoped.filter(status__in=ON_ROLL).exclude(
            enrolments__is_active=True,
        ).distinct().count()  # distinct on the rows here: no aggregate to break

        # The capacity panel is about the year being READ, not the school's
        # current one - last year's classes had last year's loads.
        session = self.session_filter or self.session_or_none
        capacity = (
            fullest_classes(
                self.tenant, request.user, session, branch=self.branch_filter,
            )
            if session else []
        )
        return success_response(data={
            "total": sum(by_status.values()),
            "on_roll": sum(by_status.get(s, 0) for s in ON_ROLL),
            "active": by_status.get(StudentStatus.ACTIVE, 0),
            "applicants": by_status.get(StudentStatus.APPLICANT, 0),
            "unassigned": unassigned,
            "by_status": [
                {"status": value, "label": StudentStatus(value).label,
                 "count": by_status.get(value, 0)}
                for value in StudentStatus.values
            ],
            "nearest_capacity": capacity,
            "session": str(session) if session else "",
            # Status, guardians and documents carry no year, so under a session
            # lens these counts describe THIS YEAR'S students as they stand
            # today. The screen has to say so rather than implying it knows who
            # was suspended in 2026.
            "status_is_current": self.session_filter is not None,
        })
