"""The pupil portal's routes.

Every view here answers for the signed-in pupil and nobody else: no route
carries a pupil's id, so there is nothing to change in an address to reach
another child. ``pending_tenant_surface`` is left at its closed default,
because a school that has not gone live has no pupils using the portal.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response

from .permissions import PORTAL_PERMISSIONS, linked_student


class PortalView(APIView):
    """What every portal view shares: the permissions, and the pupil."""

    permission_classes = PORTAL_PERMISSIONS

    @property
    def student(self):
        return linked_student(self.request)


def _initials(student) -> str:
    return "".join(
        part[0] for part in (student.first_name, student.last_name) if part
    ).upper()


class MeView(PortalView):
    """GET /v1/portal/student/me/

    Who the signed-in pupil is, for the portal's shell: their name and face,
    class and admission number, their school, their branch where the school
    has more than one, and the year and term the school is in. In the holiday
    after a promotion the class is next year's, ``class_is_next_year`` is true
    and ``session`` names that year. A school app
    asks this after sign-in: 200 means a pupil, 404 means anybody else.

    docstring-name: The signed-in pupil
    """

    def get(self, request):
        from schools.vs_gradebook.services.resolve import current_term
        from schools.vs_students.services.documents import face_url
        from schools.vs_students.services.scoping import branch_dimension_applies
        from vs_config.clock import branch_today
        from vs_user.serializers import school_public_info

        from .services import active_enrolment

        student = self.student
        tenant = student.tenant
        enrolment = active_enrolment(student)
        term = current_term(tenant, branch_today(tenant, student.branch_id))
        show_branch = branch_dimension_applies(tenant)
        school = school_public_info(
            getattr(tenant, "school_profile", None), request,
        ) or {}

        data = {
            "student": {
                "id": student.pk,
                "first_name": student.first_name,
                "full_name": student.full_name,
                "initials": _initials(student),
                "photo_url": face_url(student, request=request) or "",
                "admission_number": student.student_number or "",
                "class_name": enrolment.school_class.name if enrolment else "",
                "level_name": (
                    enrolment.school_class.level.name
                    if enrolment and enrolment.school_class.level_id else ""
                ),
                # The class is next year's in the holiday after a promotion.
                "class_is_next_year": bool(
                    enrolment and enrolment.session.status != "ACTIVE"
                ),
            },
            "school": {"name": school.get("name", ""), "logo_url": school.get("logo") or ""},
            "show_branch": show_branch,
            "session": (
                {"id": enrolment.session.pk, "name": enrolment.session.name}
                if enrolment else None
            ),
            "term": {"id": term.pk, "name": term.name} if term is not None else None,
        }
        if show_branch:
            data["branch"] = {"id": student.branch_id, "name": student.branch.name}
        return success_response(data=data)


class HomeView(PortalView):
    """GET /v1/portal/student/home/

    The pupil's home in one read: today's classes with now / next / done,
    today's attendance mark and the term's rate, the fee balance and the
    invoice falling due first, the newest notice with the unread count, and
    the unread notification count. Each block carries ``available``; one that
    cannot be read answers ``false`` with a sentence to show, never a zero,
    and the rest still arrive.

    docstring-name: My home
    """

    def get(self, request):
        from .home import home

        return success_response(data=home(self.student, user=request.user))


def _read_ids(user, notices) -> set:
    from schools.vs_notices.models import NoticeRead

    return set(
        NoticeRead.objects.filter(user=user, notice__in=notices)
        .values_list("notice_id", flat=True)
    )


class NoticeFeedView(PortalView):
    """GET /v1/portal/student/notices/

    The published notices addressed to the pupil, newest first, each marked
    unread until they open it, with the unread count for the badge. A
    withdrawn notice has left the feed.

    docstring-name: My notices
    """

    def get(self, request):
        from core.pagination import XVSPagination
        from schools.vs_notices.serializers import PortalNoticeSerializer
        from schools.vs_notices.services.notices import pupil_notices

        notices = pupil_notices(self.student)
        unread = notices.exclude(reads__user=request.user).count()
        paginator = XVSPagination()
        page = paginator.paginate_queryset(notices, request, view=self)
        data = PortalNoticeSerializer(
            page, many=True, context={"read_ids": _read_ids(request.user, page)},
        ).data
        response = paginator.get_paginated_response(data)
        response.data["unread_count"] = unread
        return response


class _OneNotice:
    """Finds one notice in the pupil's own feed, or answers 404."""

    def notice(self, pk):
        from rest_framework.exceptions import NotFound
        from schools.vs_notices.services.notices import pupil_notices

        row = pupil_notices(self.student).filter(pk=pk).first()
        if row is None:
            raise NotFound("No such notice for you.")
        return row


class NoticeDetailView(_OneNotice, PortalView):
    """GET /v1/portal/student/notices/<id>/

    One notice addressed to the pupil. Opening it does not mark it read; the
    pupil marks it, as the design has them do. A notice not addressed to them,
    or withdrawn, answers 404.

    docstring-name: One of my notices
    """

    def get(self, request, pk):
        from schools.vs_notices.serializers import PortalNoticeSerializer

        notice = self.notice(pk)
        return success_response(
            data=PortalNoticeSerializer(
                notice, context={"read_ids": _read_ids(request.user, [notice])},
            ).data,
        )


class NoticeReadView(_OneNotice, PortalView):
    """POST /v1/portal/student/notices/<id>/read/

    Marks one notice read, and clears its bell entry.

    docstring-name: Mark a notice read
    """

    def post(self, request, pk):
        from schools.vs_notices.serializers import PortalNoticeSerializer
        from schools.vs_notices.services.notices import mark_read

        notice = self.notice(pk)
        mark_read(notice, request.user)
        return success_response(
            "Marked as read.",
            data=PortalNoticeSerializer(notice, context={"read_ids": {notice.pk}}).data,
        )


class NoticeReadAllView(PortalView):
    """POST /v1/portal/student/notices/read-all/

    Marks every notice in the pupil's feed read.

    docstring-name: Mark all my notices read
    """

    def post(self, request):
        from schools.vs_notices.services.notices import mark_all_read, pupil_notices

        marked = mark_all_read(pupil_notices(self.student), request.user)
        return success_response(
            "All notices marked as read.", data={"marked": marked, "unread_count": 0},
        )


class ProfileView(PortalView):
    """GET, PATCH /v1/portal/student/profile/

    GET is the pupil's own record: what the school manages (name, admission
    number, class, date of birth, school email, primary guardian) and what
    they may change. PATCH takes ``phone`` and ``email``, either or both, and
    answers with the profile as it now stands. A value that cannot be stored
    is refused with a sentence keyed on its field.

    docstring-name: My profile
    """

    def get(self, request):
        from .services import profile

        return success_response(data=profile(self.student, request=request))

    def patch(self, request):
        from .services import profile, update_contact

        student = update_contact(
            self.student, actor=request.user,
            phone=request.data.get("phone"), email=request.data.get("email"),
        )
        return success_response("Profile updated.", data=profile(student, request=request))


class ProfilePhotoView(PortalView):
    """POST, DELETE /v1/portal/student/profile/photo/

    Replaces or removes the pupil's photograph, where the school lets pupils
    do that (``students.portal.photo_self_edit``); otherwise 403
    ``PHOTO_SELF_EDIT_OFF``. POST is ``multipart/form-data`` with one image in
    ``file``, checked as a staff upload of a passport photograph is.

    docstring-name: My photograph
    """

    def post(self, request):
        from rest_framework.exceptions import ValidationError
        from schools.vs_students.serializers import check_upload

        from .services import profile, set_photo

        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Choose a photograph to upload."})
        check_upload(upload, field="file", must_be_image=True, subject="A photograph")
        set_photo(self.student, actor=request.user, upload=upload)
        return success_response(
            "Photograph updated.", data=profile(self.student, request=request), status=201,
        )

    def delete(self, request):
        from .services import profile, remove_photo

        remove_photo(self.student, actor=request.user)
        return success_response("Photograph removed.", data=profile(self.student, request=request))


class TimetableView(PortalView):
    """GET /v1/portal/student/timetable/

    The pupil's class timetable for the active year, with today's periods
    marked done, now, next or later. ``published: false`` with no days when
    the school has not published it.

    docstring-name: My timetable
    """

    def get(self, request):
        from .services import timetable

        return success_response(data=timetable(self.student))


class AttendanceView(PortalView):
    """GET /v1/portal/student/attendance/?term=<id>

    The pupil's attendance for a term (the current one by default): counts and
    rate, one cell per school day up to today, the days worth a note (absent,
    excused and late, with the reason, note and arrival time), and today's
    cell. A term that is not this school's answers 404.

    docstring-name: My attendance
    """

    def get(self, request):
        from schools.vs_attendance.services.student_tab import pupil_attendance

        from rest_framework.exceptions import NotFound

        term = request.query_params.get("term") or None
        if term is not None and not str(term).isdigit():
            raise NotFound("No such term at this school.")
        return success_response(
            data=pupil_attendance(self.student.tenant, self.student, term_id=term),
        )


class ResultsView(PortalView):
    """GET /v1/portal/student/results/?term=<id>

    One term's released result (the latest released term by default): each
    subject with its teacher, component marks, total, grade and remark; the
    overall average; position and class average where the school shows them;
    the class teacher's remark. ``state`` is ``released``, ``withheld`` (with
    a sentence and no figures) or ``not_published``. ``terms`` lists every
    published term with its state, for the term picker.

    docstring-name: My results
    """

    def get(self, request):
        from .results import term_result

        return success_response(
            data=term_result(self.student, request.query_params.get("term") or None),
        )


class SessionResultsView(PortalView):
    """GET /v1/portal/student/results/session/?session=<id>

    The school year (the latest with a published result by default): each
    term's average, position and class average, and, once the year's last term
    is released, every subject across the year with its annual average, the
    annual average and position, the promotion decision and the session remark.

    docstring-name: My results for the year
    """

    def get(self, request):
        from .results import session_results

        return success_response(
            data=session_results(self.student, request.query_params.get("session") or None),
        )


class ReportCardView(PortalView):
    """GET /v1/portal/student/results/report-card/?term=<id>

    The released report card for a term, exactly as the gradebook prints it,
    for the portal to show and print. A held or unpublished term answers 404.
    The card of a year's last term carries the cumulative block, so it is also
    the annual card.

    docstring-name: My report card
    """

    def get(self, request):
        from .results import report_card

        return success_response(
            data=report_card(
                self.student, request.query_params.get("term") or None, user=request.user,
            ),
        )


class FeesView(PortalView):
    """GET /v1/portal/student/fees/

    The pupil's balance, what was billed and paid, and each invoice with its
    number, title, dates, lines and status, all in kobo; ``can_pay`` and
    ``min_part_payment`` are the school's rules for paying here. 503
    ``FINANCE_UNAVAILABLE`` when the books cannot be read, never a zero.

    docstring-name: My fees
    """

    def get(self, request):
        from .fees import fee_summary

        return success_response(data=fee_summary(self.student))


class PaymentHistoryView(PortalView):
    """GET /v1/portal/student/fees/payments/

    The pupil's payments, newest first: confirmed ones with a receipt, and
    online ones still being confirmed, each with how it was paid and who
    started it.

    docstring-name: My payments
    """

    def get(self, request):
        from .fees import payment_history

        return success_response(data=payment_history(self.student))


class TransferAccountView(PortalView):
    """GET, POST /v1/portal/student/fees/transfer-account/

    The pupil's permanent account for paying fees by bank transfer. GET
    answers ``state`` ``ISSUED`` (``bank_name``, ``account_number``,
    ``account_name``, ``as_of``), ``NONE`` (POST to issue one) or
    ``UNAVAILABLE`` with a sentence to show. POST issues the account, or
    returns the one the pupil has; it is rate limited. 403
    ``STUDENT_PAYMENT_OFF`` where the school does not let pupils pay; 503
    ``PROVIDER_UNAVAILABLE`` when the provider is down.

    docstring-name: My bank transfer account
    """

    def get_throttles(self):
        if self.request.method == "POST":
            from rest_framework.throttling import ScopedRateThrottle

            self.throttle_scope = "portal_transfer_account"
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get(self, request):
        from .fees import transfer_account

        return success_response(data=transfer_account(self.student))

    def post(self, request):
        from .fees import open_transfer_account

        return success_response(data=open_transfer_account(self.student, actor=request.user))


class TransferDepositsView(PortalView):
    """GET /v1/portal/student/fees/transfer-account/deposits/?since=<as_of>

    Transfers into the pupil's account since the ``as_of`` the account read
    answered, oldest first: ``PENDING`` (``Arriving``) as soon as the provider
    reports one, ``CONFIRMED`` (``Received``) once its receipt is booked. The
    page polls this while it shows the account, then re-reads ``fees/``.

    docstring-name: Transfers into my account
    """

    def get(self, request):
        from .fees import transfer_deposits

        return success_response(
            data=transfer_deposits(self.student, request.query_params.get("since")),
        )


class PayInvoiceView(PortalView):
    """POST /v1/portal/student/fees/invoices/<invoice_ref>/pay/

    Starts a payment for one of the pupil's own invoices and answers with the
    gateway checkout to send them to. ``amount`` is kobo: the whole balance, or
    a part of at least the school's smallest part payment. Refusals:
    403 ``STUDENT_PAYMENT_OFF``, 404 for an invoice not on the pupil's account,
    400 ``AMOUNT_BELOW_MINIMUM`` / ``AMOUNT_ABOVE_BALANCE`` /
    ``PAYMENT_AMOUNT_INVALID``, 409 ``INVOICE_NOT_PAYABLE``, 503
    ``PROVIDER_UNAVAILABLE``. Card and bank details are entered on the
    gateway's own page, never here. Its status is read on the public
    collection status route with the returned ``reference``.

    docstring-name: Pay my invoice
    """

    def post(self, request, invoice_ref):
        from .fees import start_payment

        return success_response(
            "Continue to the secure payment page.",
            data=start_payment(
                self.student, actor=request.user, invoice_ref=invoice_ref,
                amount=request.data.get("amount"),
            ),
            status=201,
        )


class ReceiptView(PortalView):
    """GET /v1/portal/student/fees/payments/<payment_ref>/receipt/?as=pdf

    A confirmed payment's receipt on the pupil's account: JSON for the screen,
    or with ``as=pdf`` the PDF the school's receipt email attaches. (Not
    ``format``, which DRF reserves for choosing a renderer.) The
    balance it states is the one left right after the payment. 404 for a
    payment not confirmed or not the pupil's.

    docstring-name: My receipt
    """

    def get(self, request, payment_ref):
        from django.http import HttpResponse

        from .fees import receipt, receipt_pdf

        if request.query_params.get("as") == "pdf":
            body = receipt_pdf(self.student, payment_ref)
            response = HttpResponse(body, content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="receipt-{payment_ref}.pdf"'
            return response
        return success_response(data=receipt(self.student, payment_ref))


class _PublicAppView(APIView):
    """Read before anyone signs in: a phone fetches these to install the app."""

    from rest_framework.permissions import AllowAny

    authentication_classes = []
    permission_classes = [AllowAny]
    tenant_param_required = False
    throttle_scope = "school_brand"

    def school(self, slug):
        from rest_framework.exceptions import NotFound

        from .app_manifest import school_for

        found = school_for(slug)
        if found is None:
            raise NotFound("No school for this address.")
        return found


class StudentAppManifestView(_PublicAppView):
    """GET /v1/portal/student/app/<slug>/manifest.webmanifest

    The web app manifest a phone installs the student app from: the school's
    name, and icons cut from its crest, or the app's own XVS icons where it
    has none. The school's host serves it at ``/student/manifest.webmanifest``
    by passing that address through here. 404 for a slug that names no school
    able to sign in.

    docstring-name: Student app manifest
    """

    def get(self, request, slug):
        import json

        from django.http import HttpResponse
        from django.urls import reverse

        from .app_manifest import manifest

        school = self.school(slug)
        body = manifest(school, icon_url=lambda name: request.build_absolute_uri(
            reverse("student-app-icon", args=[school.slug, name]),
        ))
        response = HttpResponse(json.dumps(body), content_type="application/manifest+json")
        response["Cache-Control"] = "public, max-age=3600"
        return response


class StudentAppIconView(_PublicAppView):
    """GET /v1/portal/student/app/<slug>/icons/<name>

    One home-screen icon cut from the school's crest: ``icon-192.png``,
    ``icon-512.png``, ``maskable-512.png`` or ``apple-touch-icon.png``. 404
    where the school has no crest, which the manifest never points at.

    docstring-name: Student app icon
    """

    def get(self, request, slug, name):
        from django.http import HttpResponse
        from rest_framework.exceptions import NotFound

        from .app_manifest import icon

        found = icon(self.school(slug), name)
        if found is None:
            raise NotFound("No such icon for this school.")
        response = HttpResponse(found, content_type="image/png")
        response["Cache-Control"] = "public, max-age=86400"
        response["X-Content-Type-Options"] = "nosniff"
        return response
