"""The parent portal's routes.

Every view here answers for the signed-in parent and their own children. A
route that names a child resolves it against the parent's children on every
request, so another family's child and a child that does not exist answer with
the same 404. ``pending_tenant_surface`` is left at its closed default,
because a school that has not gone live has no parents using the portal.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response
from schools.vs_student_portal.views import PublicAppView

from .permissions import PORTAL_PERMISSIONS, child_of, children_of, linked_guardian


class PortalView(APIView):
    """What every portal view shares: the permissions, and the guardian."""

    permission_classes = PORTAL_PERMISSIONS

    @property
    def guardian(self):
        return linked_guardian(self.request)

    @property
    def children(self):
        return children_of(self.request)


class ChildView(PortalView):
    """A portal view of one child, named by ``child_id`` in the address."""

    @property
    def child(self):
        return child_of(self.request, self.kwargs["child_id"])


class MeView(PortalView):
    """GET /v1/portal/parent/me/

    Who the signed-in parent is, for the portal's shell: their name, their
    school, the year and term the school is in, and each of their children on
    the roll with name, face, class, admission number and the parent's
    relationship to them. A child's branch appears only at a school with more
    than one branch. A school app asks this after sign-in: 200 means a parent,
    404 means anybody else.

    docstring-name: The signed-in parent
    """

    def get(self, request):
        from .services import me

        return success_response(data=me(self.guardian, self.children, request=request))


class ProfileView(PortalView):
    """GET, PATCH /v1/portal/parent/profile/

    GET is the parent's own profile: what the school manages (name, the address
    they sign in with, their children on the roll) and what they may change
    (``phone``, ``alt_phone`` and ``address``), with ``email_editable: false``.
    PATCH takes any of the three editable fields and answers with the profile as
    it now stands. A value that cannot be stored is refused with a sentence
    keyed on its field, and any other field, ``email`` and ``first_name``
    included, is refused 400 with "This cannot be changed here. Ask the school."
    The change is audited with the parent as its actor.

    docstring-name: My profile as a parent
    """

    def get(self, request):
        from .profile import profile

        return success_response(
            data=profile(self.guardian, self.children, user=request.user, request=request),
        )

    def patch(self, request):
        from .profile import profile, update_contact

        guardian = update_contact(self.guardian, request.data, actor=request.user)
        return success_response(
            "Profile updated.",
            data=profile(guardian, self.children, user=request.user, request=request),
        )


class ProfilePhotoView(PortalView):
    """POST, DELETE /v1/portal/parent/profile/photo/

    Replaces or removes the parent's own photograph, the one the school sees on
    their guardian record. POST is ``multipart/form-data`` with one image in
    ``file``, checked as a staff upload of a guardian photograph is. Both answer
    with the profile as it now stands, and both are audited with the parent as
    the actor.

    docstring-name: My photograph as a parent
    """

    def post(self, request):
        from rest_framework.exceptions import ValidationError

        from schools.vs_students.serializers import check_upload
        from schools.vs_students.services.guardians import set_photo

        from .profile import profile

        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Choose a photograph to upload."})
        check_upload(upload, field="file", must_be_image=True, subject="A photograph")
        guardian = set_photo(self.guardian, upload=upload, actor=request.user)
        return success_response(
            "Photograph updated.",
            data=profile(guardian, self.children, user=request.user, request=request),
            status=201,
        )

    def delete(self, request):
        from schools.vs_students.services.guardians import clear_photo

        from .profile import profile

        guardian = self.guardian
        if guardian.photo:
            guardian = clear_photo(guardian, actor=request.user)
        return success_response(
            "Photograph removed.",
            data=profile(guardian, self.children, user=request.user, request=request),
        )


class HomeView(PortalView):
    """GET /v1/portal/parent/home/

    The parent's home in one read. For each child: their class, and a block each
    for attendance (the term's rate and today's mark), fees (balance, status
    ``OWING`` / ``CLEAR`` / ``IN_CREDIT``, open invoices), results (the latest
    released term and its overall average, or none released) and today (the
    lesson in progress or next). For the family: fees (what is owed in all and
    the total of paying every open invoice), notices (the newest one, with the
    children it is for, and the unread count) and notifications. Every block
    carries ``available``; one that cannot be read answers ``available: false``
    with a sentence and the rest still arrive.

    docstring-name: My family's home
    """

    def get(self, request):
        from .home import home

        return success_response(data=home(self.guardian, self.children, user=request.user))


class ChildRecordView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/

    One child's record as a parent may see it: name, admission number, class,
    year, date of birth, photograph, and the other guardians on the record by
    name and relationship only. Another family's child answers 404.

    docstring-name: My child
    """

    def get(self, request, child_id):
        from .services import child_record

        return success_response(
            data=child_record(self.guardian, self.child, request=request),
        )


class TimetableView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/timetable/

    The child's class timetable for the active year, as the pupil sees it.
    ``published: false`` with no days when the school has not published it.

    docstring-name: My child's timetable
    """

    def get(self, request, child_id):
        from schools.vs_student_portal.services import timetable

        return success_response(data=timetable(self.child))


class AttendanceView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/attendance/?term=<id>

    The child's attendance for a term (the current one by default), as the
    pupil sees it. A term that is not this school's answers 404.

    docstring-name: My child's attendance
    """

    def get(self, request, child_id):
        from schools.vs_attendance.services.student_tab import pupil_attendance

        term = request.query_params.get("term") or None
        if term is not None and not str(term).isdigit():
            raise NotFound("No such term at this school.")
        child = self.child
        return success_response(data=pupil_attendance(child.tenant, child, term_id=term))


class ResultsView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/results/?term=<id>

    One term's released result for the child (the latest released term by
    default), exactly as the pupil reads it: ``released``, ``withheld`` or
    ``not_published``.

    docstring-name: My child's results
    """

    def get(self, request, child_id):
        from schools.vs_student_portal.results import term_result

        return success_response(
            data=term_result(self.child, request.query_params.get("term") or None),
        )


class SessionResultsView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/results/session/?session=<id>

    The child's results across a school year (the latest with a published
    result by default), as the pupil reads them.

    docstring-name: My child's results for the year
    """

    def get(self, request, child_id):
        from schools.vs_student_portal.results import session_results

        return success_response(
            data=session_results(self.child, request.query_params.get("session") or None),
        )


class ReportCardView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/results/report-card/?term=<id>

    The released report card for a term, exactly as the gradebook prints it.
    A held or unpublished term answers 404.

    docstring-name: My child's report card
    """

    def get(self, request, child_id):
        from schools.vs_student_portal.results import report_card

        return success_response(
            data=report_card(
                self.child, request.query_params.get("term") or None, user=request.user,
            ),
        )


class FeesView(PortalView):
    """GET /v1/portal/parent/fees/

    Every child's fees: balance, billed, paid and each invoice with its number,
    title, dates, lines and status, all in kobo. A child whose books cannot be
    read carries ``available: false`` and a sentence, never zeros. ``combined``
    sums billed, paid and outstanding across the children, and is itself
    ``available: false`` while any child cannot be read. ``min_part_payment`` is
    the school's smallest part payment; the whole balance of an invoice may
    always be paid. ``pay_all`` lists one step per open invoice, by child and
    then oldest due date first: the payments engine charges one child's account
    per checkout, so the app runs one checkout for each step in turn.

    docstring-name: My children's fees
    """

    def get(self, request):
        from .fees import family_fees

        return success_response(data=family_fees(self.guardian, self.children))


class PaymentHistoryView(PortalView):
    """GET /v1/portal/parent/fees/payments/

    Every child's payments merged, newest first, each naming its child:
    confirmed ones with a receipt and online ones still being confirmed. 503
    ``FINANCE_UNAVAILABLE`` when any child's history cannot be read.

    docstring-name: My children's payments
    """

    def get(self, request):
        from .fees import payment_history

        return success_response(data=payment_history(self.children))


class ReceiptView(PortalView):
    """GET /v1/portal/parent/fees/payments/<payment_ref>/receipt/?as=pdf

    A confirmed payment's receipt on one of the parent's children's accounts:
    JSON for the screen, or with ``as=pdf`` the PDF the school's receipt email
    attaches. (Not ``format``, which DRF reserves for choosing a renderer.) 404
    for a payment not confirmed or not a child of this parent's.

    docstring-name: My children's receipt
    """

    def get(self, request, payment_ref):
        from django.http import HttpResponse

        from .fees import receipt, receipt_pdf

        if request.query_params.get("as") == "pdf":
            response = HttpResponse(
                receipt_pdf(self.children, payment_ref), content_type="application/pdf",
            )
            response["Content-Disposition"] = f'attachment; filename="receipt-{payment_ref}.pdf"'
            return response
        return success_response(data=receipt(self.children, payment_ref))


class InvoiceDetailView(PortalView):
    """GET /v1/portal/parent/fees/invoices/<invoice_ref>/?as=pdf

    One invoice on a child's account: JSON for the screen (number, dates, lines,
    balance, status, the child, and the printed ``document`` the sheet draws),
    or with ``as=pdf`` the PDF the school sends.
    404 for an invoice that is not on one of the parent's children's accounts.

    docstring-name: My children's invoice
    """

    def get(self, request, invoice_ref):
        from django.http import HttpResponse

        from .fees import invoice_detail, invoice_pdf

        if request.query_params.get("as") == "pdf":
            response = HttpResponse(
                invoice_pdf(self.children, invoice_ref), content_type="application/pdf",
            )
            response["Content-Disposition"] = f'attachment; filename="invoice-{invoice_ref}.pdf"'
            return response
        return success_response(data=invoice_detail(self.children, invoice_ref))


class PayInvoiceView(PortalView):
    """POST /v1/portal/parent/fees/invoices/<invoice_ref>/pay/

    Starts a payment for one invoice of one of the parent's children and answers
    with the gateway checkout to send them to. ``amount`` is kobo: the whole
    balance, or a part of at least the school's smallest part payment. A parent
    may always pay for their own children. Refusals: 404 for an invoice that is
    not a child's of this parent, 400 ``AMOUNT_BELOW_MINIMUM`` /
    ``AMOUNT_ABOVE_BALANCE`` / ``PAYMENT_AMOUNT_INVALID``, 409
    ``INVOICE_NOT_PAYABLE``, 503 ``PROVIDER_UNAVAILABLE``. Card and bank details
    are entered on the gateway's own page, never here. Its status is read on the
    public collection status route with the returned ``reference``.

    docstring-name: Pay my child's invoice
    """

    def post(self, request, invoice_ref):
        from .fees import start_payment

        return success_response(
            "Continue to the secure payment page.",
            data=start_payment(
                self.guardian, self.children, user=request.user,
                invoice_ref=invoice_ref, amount=request.data.get("amount"),
            ),
            status=201,
        )


class TransferAccountView(ChildView):
    """GET, POST /v1/portal/parent/children/<child_id>/fees/transfer-account/

    The child's permanent account for paying fees by bank transfer; each pupil
    has one, and there is none for a parent. GET answers ``state`` ``ISSUED``
    (``bank_name``, ``account_number``, ``account_name``, ``as_of``), ``NONE``
    (POST to issue one) or ``UNAVAILABLE`` with a sentence to show. POST issues
    the account, or returns the one the child has; it is rate limited. Offered
    whether or not the school lets pupils pay for themselves. 503
    ``PROVIDER_UNAVAILABLE`` when the provider is down.

    docstring-name: My child's bank transfer account
    """

    def get_throttles(self):
        if self.request.method == "POST":
            from rest_framework.throttling import ScopedRateThrottle

            self.throttle_scope = "portal_transfer_account"
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get(self, request, child_id):
        from .fees import transfer_account

        return success_response(data=transfer_account(self.child))

    def post(self, request, child_id):
        from .fees import open_transfer_account

        return success_response(data=open_transfer_account(self.child, actor=request.user))


class TransferDepositsView(ChildView):
    """GET /v1/portal/parent/children/<child_id>/fees/transfer-account/deposits/?since=<as_of>

    Transfers into the child's account since the ``as_of`` the account read
    answered, oldest first: ``PENDING`` (``Arriving``) as soon as the provider
    reports one, ``CONFIRMED`` (``Received``) once its receipt is booked. The
    page polls this while it shows the account, then re-reads ``fees/``.

    docstring-name: Transfers into my child's account
    """

    def get(self, request, child_id):
        from .fees import transfer_deposits

        return success_response(
            data=transfer_deposits(self.child, request.query_params.get("since")),
        )


def _read_ids(user, notices) -> set:
    from schools.vs_notices.models import NoticeRead

    return set(
        NoticeRead.objects.filter(user=user, notice__in=notices)
        .values_list("notice_id", flat=True)
    )


def _children_for(notices, children, placements) -> dict:
    """For each notice, the first names of the children it is addressed to."""
    from schools.vs_notices.services.notices import children_addressed

    return {
        n.pk: [c.first_name for c in children_addressed(n, children, placements)]
        for n in notices
    }


class _Feed:
    """The notices addressed to any of the parent's children."""

    def feed(self):
        from schools.vs_notices.services.notices import guardian_notices, pupil_placements

        placements = pupil_placements(self.children)
        return guardian_notices(self.children, placements=placements), placements

    def context(self, notices, placements):
        return {
            "read_ids": _read_ids(self.request.user, notices),
            "children_for": _children_for(notices, self.children, placements),
        }


class NoticeFeedView(_Feed, PortalView):
    """GET /v1/portal/parent/notices/

    The published notices addressed to any of the parent's children, newest
    first and each once, marked unread until the parent opens it, with the
    unread count for the badge and, on each notice, the first names of the
    children it is for. Read marks belong to the parent's account.

    docstring-name: My notices as a parent
    """

    def get(self, request):
        from core.pagination import XVSPagination

        from .serializers import ParentNoticeSerializer

        notices, placements = self.feed()
        unread = notices.exclude(reads__user=request.user).count()
        paginator = XVSPagination()
        page = paginator.paginate_queryset(notices, request, view=self)
        data = ParentNoticeSerializer(
            page, many=True, context=self.context(page, placements),
        ).data
        response = paginator.get_paginated_response(data)
        response.data["unread_count"] = unread
        return response


class _OneNotice(_Feed):
    """Finds one notice in the parent's own feed, or answers 404."""

    def notice(self, pk):
        notices, placements = self.feed()
        row = notices.filter(pk=pk).first()
        if row is None:
            raise NotFound("No such notice for you.")
        return row, placements


class NoticeDetailView(_OneNotice, PortalView):
    """GET /v1/portal/parent/notices/<id>/

    One notice addressed to any of the parent's children. Opening it does not
    mark it read. A notice not addressed to them, or withdrawn, answers 404.

    docstring-name: One of my notices as a parent
    """

    def get(self, request, pk):
        from .serializers import ParentNoticeSerializer

        notice, placements = self.notice(pk)
        return success_response(
            data=ParentNoticeSerializer(
                notice, context=self.context([notice], placements),
            ).data,
        )


class NoticeReadView(_OneNotice, PortalView):
    """POST /v1/portal/parent/notices/<id>/read/

    Marks one notice read for the parent, and clears its bell entry.

    docstring-name: Mark a notice read as a parent
    """

    def post(self, request, pk):
        from schools.vs_notices.services.notices import mark_read

        from .serializers import ParentNoticeSerializer

        notice, placements = self.notice(pk)
        mark_read(notice, request.user)
        context = self.context([notice], placements)
        context["read_ids"] = {notice.pk}
        return success_response(
            "Marked as read.", data=ParentNoticeSerializer(notice, context=context).data,
        )


class NoticeReadAllView(_Feed, PortalView):
    """POST /v1/portal/parent/notices/read-all/

    Marks every notice in the parent's feed read.

    docstring-name: Mark all my notices read as a parent
    """

    def post(self, request):
        from schools.vs_notices.services.notices import mark_all_read

        notices, _ = self.feed()
        marked = mark_all_read(notices, request.user)
        return success_response(
            "All notices marked as read.", data={"marked": marked, "unread_count": 0},
        )


class ParentAppManifestView(PublicAppView):
    """GET /v1/portal/parent/app/<slug>/manifest.webmanifest

    The web app manifest a phone installs the parent app from: the school's
    name, and icons cut from its crest, or the parent app's own XVS icons where
    it has none, opening at ``/parent/``. The school's host serves it by passing
    ``/parent/app/<slug>/manifest.webmanifest`` through here; a crest icon's
    address is relative to the manifest, so it is right either way. 404 for a
    slug that names no school able to sign in.

    docstring-name: Parent app manifest
    """

    def get(self, request, slug):
        import json

        from django.http import HttpResponse

        from schools.vs_student_portal.app_manifest import PARENT_APP, manifest

        body = manifest(self.school(slug), icon_url=lambda name: f"icons/{name}", app=PARENT_APP)
        response = HttpResponse(json.dumps(body), content_type="application/manifest+json")
        response["Cache-Control"] = "public, max-age=3600"
        return response


class ParentAppIconView(PublicAppView):
    """GET /v1/portal/parent/app/<slug>/icons/<name>

    One home-screen icon cut from the school's crest, the same drawing the
    student app uses. 404 where the school has no crest, which the manifest
    never points at.

    docstring-name: Parent app icon
    """

    def get(self, request, slug, name):
        from django.http import HttpResponse

        from schools.vs_student_portal.app_manifest import icon

        found = icon(self.school(slug), name)
        if found is None:
            raise NotFound("No such icon for this school.")
        response = HttpResponse(found, content_type="image/png")
        response["Cache-Control"] = "public, max-age=86400"
        response["X-Content-Type-Options"] = "nosniff"
        return response
