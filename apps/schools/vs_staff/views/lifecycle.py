"""The employment lifecycle, the account actions, and the history.

Two vocabularies, kept apart on purpose. An employment transition says whether
somebody still works here and is this module's; an account action says whether a
login may be used and is the identity layer's. Each surface here does one of
them, and the response says in words what it did to the other.

FRD M12 v2.1, FR-008, FR-009, FR-012 and FR-020.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_rbac.field_enforcement import assert_writable

from ..constants import (
    PERM_ACCOUNT_REACTIVATE,
    PERM_CREATE,
    PERM_ACCOUNT_SUSPEND,
    PERM_ACCOUNT_UPDATE,
    PERM_TRANSITION,
    PERM_VIEW,
)
from ..serializers import (
    EmailChangeSerializer,
    EmploymentEventSerializer,
    RevokeInvitationSerializer,
    StaffDetailSerializer,
    StatusChangeSerializer,
)
from ..services import accounts, employment, invitations
from ..services.visibility import GROUP_HISTORY
from .base import StaffViewMixin


class StaffStatusView(StaffViewMixin, APIView):
    """POST /v1/i/me/staff/<id>/status/ - move somebody through the lifecycle.

    Closed before go-live. Nobody resigns during onboarding, and a closed
    surface is the safer default for a SENSITIVE key.

    The response carries the classes that now need cover, **named and not
    counted**. "3 classes need cover" sends a head teacher hunting through a
    roster of a hundred and nine; "JSS1 A Mathematics, JSS1 B Mathematics, SSS2
    Physics" does not.

    docstring-name: Change an employment status
    """

    rbac_permission = PERM_TRANSITION

    def get(self, request, pk):
        """What this person can be moved to, and what each move would do.

        The drawer reads this rather than hard-coding a transition table, so a
        rule changed in the service reaches the screen without a release.
        """
        staff = self.get_staff(pk)
        return success_response(data={
            "employment_status": staff.employment_status,
            "account_status": staff.user.account_state,
            # Null unless the empty list needs explaining, which is what stops
            # the drawer inventing a reason of its own.
            "note": employment.transitions_note(staff, request.user),
            "options": [
                {
                    "value": value,
                    "label": dict(
                        employment.EmploymentStatus.choices,
                    )[value],
                    "account_effect": employment.ACCOUNT_EFFECT_TEXT[value],
                    "reason_required": value in employment.REASON_REQUIRED_FOR,
                    "last_working_day_required": (
                        value in employment.LAST_WORKING_DAY_REQUIRED_FOR
                    ),
                }
                for value in employment.allowed_transitions(staff, request.user)
            ],
            "assignments_needing_cover": employment.assignments_needing_cover(staff),
        })

    def post(self, request, pk):
        staff = self.get_staff_for_write(pk)
        payload = StatusChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        # Read before the move: afterwards a terminated person's assignments are
        # still theirs, but the list is what the confirmation promised.
        cover = employment.assignments_needing_cover(staff)

        staff, event = employment.change_status(
            staff, to_status=data["to_status"], actor=request.user,
            effective_date=data.get("effective_date"),
            reason=data.get("reason", ""),
            last_working_day=data.get("last_working_day"),
            note=data.get("note", ""), request=request,
        )
        return success_response(
            message=employment.ACCOUNT_EFFECT_TEXT[data["to_status"]],
            data={
                "staff": StaffDetailSerializer(
                    self.get_staff(pk), context=self.serializer_context(),
                ).data,
                "event": EmploymentEventSerializer(event).data,
                "assignments_needing_cover": cover,
            },
        )


class StaffHistoryView(StaffViewMixin, APIView):
    """GET /v1/i/me/staff/<id>/history/ - one timeline, two halves.

    The employment events this module writes and the account events the identity
    layer writes, on one list, **visually distinguishable**: a lockout is
    rendered as a lockout and never as a suspension, because a school reading
    one as the other believes its teacher was disciplined for mistyping a
    password.

    Read under ``school.teachers.view`` with the person in the reader's
    branches, or where the school's profile policy shows history to the
    reader's standing to them (``services/visibility.py``).

    ``?as_at=YYYY-MM-DD`` answers as at the end of that day (``as_at.py``), for
    the person themselves and a reader whose key reaches them.

    docstring-name: A staff member's history
    """

    rbac_permission = PERM_VIEW
    pagination_class = XVSPagination
    profile_group = GROUP_HISTORY

    #: Account events worth a line on somebody's profile.
    #:
    #: Sign-ins are deliberately absent. A teacher signs in twice a day, and a
    #: timeline that recorded it would bury the invitation, the lockout and the
    #: suspension under six hundred rows of nothing. Who signed in when is a
    #: security question and is answered by the audit trail, which is where it
    #: belongs.
    ACCOUNT_EVENTS = (
        "USER_CREATED", "INVITATION_SENT", "ACCOUNT_ACTIVATED",
        "ACCOUNT_LOCKED", "ACCOUNT_UNLOCKED", "ACCOUNT_SUSPENDED",
        "ACCOUNT_REACTIVATED", "ACCOUNT_DEACTIVATED",
        "PASSWORD_RESET_REQUESTED", "PASSWORD_RESET_COMPLETED",
        "PASSWORD_CHANGED", "EMAIL_CHANGED",
    )

    def get(self, request, pk):
        from vs_history.as_at import parse_as_at

        from .. import as_at as past

        staff, _access, admission = self.admit_profile_read(pk)
        from ..services.visibility import ADMITTED_KEY

        show_private_notes = admission == ADMITTED_KEY
        as_at = parse_as_at(request)
        self.refuse_as_at_unless_full(as_at, admission)
        rows = staff.employment_events.select_related("changed_by")
        if as_at is not None:
            past.staff_at(staff, as_at)
            rows = rows.filter(created_at__lt=as_at.moment)
        events = []
        for row in rows:
            detail = dict(EmploymentEventSerializer(row).data)
            if not show_private_notes:
                detail.pop("reason", None)
                detail.pop("note", None)
            events.append({"kind": "employment", "at": row.created_at, **detail})
        timeline = sorted(
            events + self._account_events(staff, as_at, show_private_notes),
            key=lambda row: row["at"], reverse=True,
        )
        return success_response(data={"entries": timeline})

    def _account_events(self, staff, as_at=None, show_private_notes=False):
        """The identity layer's half, read from the audit trail it writes to.

        Every account action is recorded by ``vs_user.services.audit.
        log_auth_event`` as an identity ``AuditEvent`` about the account, with
        the auth event's name in ``metadata.auth_event``. That is the only
        record of them (see ``vs_user.auth_events``).

        Read under this view's own key, not through the audit endpoints. Those
        are gated on ``platform.audit.view``, which no school role holds, and
        the rows are narrowed here to this tenant, this person and the events
        in ``ACCOUNT_EVENTS``. Nothing from the metadata leaves except the note
        an administrator wrote on an email change: the addresses, IP and device
        stay behind, because a history is the most widely read part of a
        profile and the email is itself under Field Access.

        An actor is an id and a display name and never an email address.
        """
        from vs_audit.models import AuditEvent
        from vs_user.auth_events import AuthEvent

        labels = dict(AuthEvent.choices)
        rows = (
            AuditEvent.objects.filter(
                tenant=self.tenant, entity_type="User",
                entity_id=str(staff.user_id),
                metadata__auth_event__in=self.ACCOUNT_EVENTS,
                **({"event_at__lt": as_at.moment} if as_at is not None else {}),
            )
            .select_related("actor_user")
            .order_by("-event_at")[:100]
        )
        entries = []
        for row in rows:
            event = row.metadata.get("auth_event", "")
            actor = row.actor_user
            entries.append({
                "kind": "account",
                "at": row.event_at,
                "event": event,
                "label": labels.get(event, event),
                "note": str(row.metadata.get("note") or "") if show_private_notes and event == "EMAIL_CHANGED" else "",
                "actor": (
                    {
                        "id": actor.pk,
                        "name": " ".join(
                            part for part in (actor.first_name, actor.last_name) if part
                        ).strip(),
                    }
                    if actor is not None else None
                ),
            })
        return entries


class _AccountActionView(StaffViewMixin, APIView):
    """One account action, called through the service the platform endpoint calls.

    Nothing in this family writes ``User.status``. Each subclass names its key
    and its service call, so the eligibility rules, the auth event, the session
    blacklisting and the lockout clearing are asked once and answered once.
    """

    action = None
    message = ""

    def post(self, request, pk):
        staff = self.get_staff_for_write(pk)
        getattr(accounts, self.action)(staff, actor=request.user, request=request)
        staff.refresh_from_db()
        return success_response(
            message=self.message,
            data=StaffDetailSerializer(
                self.get_staff(pk), context=self.serializer_context(),
            ).data,
        )


class StaffAccountSuspendView(_AccountActionView):
    """POST /v1/i/me/staff/<id>/account/suspend/ - close a login.

    This does not make somebody unemployed, and the message says so: an
    administrator suspending a suspected-compromised credential is doing
    something to an account, not to a job.

    docstring-name: Suspend a staff account
    """

    rbac_permission = PERM_ACCOUNT_SUSPEND
    action = "suspend"
    message = "They cannot sign in. Their employment record is unchanged."


class StaffAccountReactivateView(_AccountActionView):
    """POST /v1/i/me/staff/<id>/account/reactivate/ - end a suspension.

    docstring-name: Reactivate a staff account
    """

    rbac_permission = PERM_ACCOUNT_REACTIVATE
    action = "reactivate"
    message = "They can sign in again."


class StaffAccountUnlockView(_AccountActionView):
    """POST /v1/i/me/staff/<id>/account/unlock/ - clear a security lockout.

    A separate action on the same key as reactivate, because the two answer
    different questions. Reactivate ends an administrative suspension; unlock
    clears a lockout after failed sign-in attempts. A school that confuses them
    is a school that thinks its teacher was suspended.

    docstring-name: Unlock a staff account
    """

    rbac_permission = PERM_ACCOUNT_REACTIVATE
    action = "unlock"
    message = "The lockout is cleared and they can sign in again."


class StaffAccountEmailView(StaffViewMixin, APIView):
    """PATCH /v1/i/me/staff/<id>/account/email/ - change the sign-in address.

    Refused for an address already in use at this school, and it reveals nothing
    about any other: the uniqueness is per tenant, so the same address may
    legitimately be an account elsewhere and a school must not learn so.

    The address is also the registered ``email`` field of ``school.teachers``,
    so a role whose Field Access leaves it read-only is refused with 403 even
    when it holds the account key. An account still waiting to be activated has
    its invitation reissued to the new address (see EmailChangeService).

    An optional ``note`` says why, and is shown on the person's history.

    docstring-name: Change a staff account's email
    """

    rbac_permission = PERM_ACCOUNT_UPDATE

    def patch(self, request, pk):
        staff = self.get_staff_for_write(pk)
        payload = EmailChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        assert_writable(request, "school.teachers", {"email": payload.validated_data["email"]})
        accounts.change_email(
            staff, payload.validated_data["email"],
            actor=request.user, request=request,
            note=payload.validated_data["note"],
        )
        return success_response(
            message="Email address changed.",
            data=StaffDetailSerializer(
                self.get_staff(pk), context=self.serializer_context(),
            ).data,
        )


class StaffResendInvitationView(StaffViewMixin, APIView):
    """POST /v1/i/me/staff/<id>/resend/ - send the invitation again.

    Open before go-live, because chasing an administrator who has not accepted
    is part of getting a school onto the platform.

    Refused for any account that has already been activated, with 422 rather
    than a silent success: an invitation that has been used is not an invitation
    any more, and telling a school one was resent when nothing was sent is worse
    than refusing. Refused too for a hire still awaiting approval
    (``HIRE_AWAITING_APPROVAL``), which has no invitation until it is approved,
    and for somebody imported during setup while the school is still being set
    up (``INVITATION_HELD_FOR_GO_LIVE``). Once the school is live, a resend
    sends that person's held invitation, which is how one left behind by the
    go-live release is invited.

    The key moved with the create. Resending is the same act as inviting, aimed
    at the same account, so it needs the same key: leaving it on
    ``school.administrators.create`` while the invite moved to
    ``school.teachers.create`` would let a branch admin invite somebody and then
    be refused when they tried to chase them.

    docstring-name: Resend a staff invitation
    """

    rbac_permission = PERM_CREATE
    pending_tenant_surface = True

    def post(self, request, pk):
        from vs_user.models import User
        from vs_user.services.invitation import InvitationService

        from ..constants import EmploymentStatus
        from ..exceptions import (
            HireAwaitingApproval,
            InvitationAlreadyAccepted,
            InvitationHeldForGoLive,
        )

        staff = self.get_staff_for_write(pk)
        if staff.employment_status == EmploymentStatus.PENDING_APPROVAL:
            raise HireAwaitingApproval()
        if staff.employment_status == EmploymentStatus.AWAITING_GO_LIVE:
            if self.onboarding:
                raise InvitationHeldForGoLive()
            from ..services.setup_invitations import release

            release(staff, actor=request.user)
            return success_response(
                message="Invitation sent.",
                data=StaffDetailSerializer(
                    self.get_staff(pk), context=self.serializer_context(),
                ).data,
            )
        if staff.user.status != User.Status.PENDING:
            raise InvitationAlreadyAccepted(
                "This invitation has already been accepted, so there is nothing "
                "to resend.",
                account_status=staff.user.status,
            )
        InvitationService.resend(
            user=staff.user, requested_by=request.user, request=request,
        )
        return success_response(
            message="Invitation sent again. The previous link no longer works.",
            data=StaffDetailSerializer(
                self.get_staff(pk), context=self.serializer_context(),
            ).data,
        )


class StaffInvitationRevokeView(StaffViewMixin, APIView):
    """POST /v1/i/me/staff/<id>/invitation/revoke/ - withdraw an unused invitation.

    The link stops working and the account is closed before it was ever signed
    into. The record survives at TERMINATED with an event recording why, because
    a school that invited the wrong address has a record of having done so, and
    deleting it is how the same mistake gets made twice.

    Nothing is emailed. There is no notification event for a withdrawn
    invitation, and inventing one would tell somebody they had been un-hired by
    a school they had not joined.

    A hire still awaiting approval has no invitation to revoke: the same call
    withdraws the hire, cancelling its approval and closing it as a refused
    hire is closed. Somebody imported during setup, whose invitation waits for
    go-live, is closed the same way.

    Open before go-live, because that is when a school loading its staff list
    finds the person who should not be on it; left closed, the one way to stop
    their invitation would be to go live and send it.

    docstring-name: Revoke a staff invitation
    """

    rbac_permission = PERM_TRANSITION
    pending_tenant_surface = ("post",)

    def post(self, request, pk):
        from ..constants import EmploymentStatus

        staff = self.get_staff_for_write(pk)
        payload = RevokeInvitationSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        status_before = staff.employment_status
        invitations.revoke(
            staff, reason=payload.validated_data["reason"],
            actor=request.user, request=request,
        )
        return success_response(
            message={
                EmploymentStatus.PENDING_APPROVAL:
                    "Hire withdrawn before it was approved. Nothing was sent to them.",
                EmploymentStatus.AWAITING_GO_LIVE:
                    "Invitation withdrawn before it was sent. Nothing was sent to them.",
            }.get(status_before, "Invitation withdrawn. The link no longer works."),
            data=StaffDetailSerializer(
                self.get_staff(pk), context=self.serializer_context(),
            ).data,
        )
