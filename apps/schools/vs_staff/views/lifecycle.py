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

from ..constants import (
    PERM_ACCOUNT_REACTIVATE,
    PERM_CREATE,
    PERM_ACCOUNT_SUSPEND,
    PERM_ACCOUNT_UPDATE,
    PERM_MANAGE,
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

    rbac_permission = PERM_MANAGE

    def get(self, request, pk):
        """What this person can be moved to, and what each move would do.

        The drawer reads this rather than hard-coding a transition table, so a
        rule changed in the service reaches the screen without a release.
        """
        staff = self.get_staff(pk)
        return success_response(data={
            "employment_status": staff.employment_status,
            "account_status": staff.user.status,
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
        staff = self.get_staff(pk)
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

    docstring-name: A staff member's history
    """

    rbac_permission = PERM_VIEW
    pagination_class = XVSPagination

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
        staff = self.get_staff(pk)
        events = [
            {
                "kind": "employment",
                "at": row.created_at,
                **EmploymentEventSerializer(row).data,
            }
            for row in staff.employment_events.select_related("changed_by")
        ]
        timeline = sorted(
            events + self._account_events(staff),
            key=lambda row: row["at"], reverse=True,
        )
        return success_response(data={"entries": timeline})

    def _account_events(self, staff):
        """The identity layer's half, read from its own log.

        Read from ``AuthEventLog`` rather than from the audit trail, and that is
        a deliberate choice about who may see it. The audit trail is gated on
        ``platform.audit.view``, which no school role holds and should not, so a
        school reading its own people's history through it would see the
        employment half and an empty space where the account half belongs. The
        auth log carries the same events, is scoped to this tenant and this
        subject, and is the identity layer's own record of them.

        An actor is an id and a display name and never an email address, which
        is the rule the platform already applies to ``created_by`` and matters
        most here, because a history is the most widely read part of a profile.
        """
        from vs_user.models import AuthEventLog

        rows = (
            AuthEventLog.objects.filter(
                tenant=self.tenant, subject_id=staff.user_id,
                event__in=self.ACCOUNT_EVENTS,
            )
            .select_related("actor")
            .order_by("-created_at")[:100]
        )
        return [
            {
                "kind": "account",
                "at": row.created_at,
                "event": row.event,
                "label": row.get_event_display(),
                "actor": (
                    {
                        "id": row.actor_id,
                        "name": " ".join(
                            part for part in (
                                row.actor.first_name, row.actor.last_name,
                            ) if part
                        ).strip(),
                    }
                    if row.actor_id else None
                ),
            }
            for row in rows
        ]


class _AccountActionView(StaffViewMixin, APIView):
    """One account action, called through the service the platform endpoint calls.

    Nothing in this family writes ``User.status``. Each subclass names its key
    and its service call, so the eligibility rules, the auth event, the session
    blacklisting and the lockout clearing are asked once and answered once.
    """

    action = None
    message = ""

    def post(self, request, pk):
        staff = self.get_staff(pk)
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

    docstring-name: Change a staff account's email
    """

    rbac_permission = PERM_ACCOUNT_UPDATE

    def patch(self, request, pk):
        staff = self.get_staff(pk)
        payload = EmailChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        accounts.change_email(
            staff, payload.validated_data["email"],
            actor=request.user, request=request,
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
    than refusing.

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

        from ..exceptions import InvitationAlreadyAccepted

        staff = self.get_staff(pk)
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

    docstring-name: Revoke a staff invitation
    """

    rbac_permission = PERM_MANAGE

    def post(self, request, pk):
        staff = self.get_staff(pk)
        payload = RevokeInvitationSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        invitations.revoke(
            staff, reason=payload.validated_data["reason"],
            actor=request.user, request=request,
        )
        return success_response(
            message="Invitation withdrawn. The link no longer works.",
            data=StaffDetailSerializer(
                self.get_staff(pk), context=self.serializer_context(),
            ).data,
        )
