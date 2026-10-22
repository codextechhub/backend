"""The names-only staff directory behind every staff picker.

``GET /v1/i/me/staff/names/?q=`` answers with the school's active staff as
``{"id", "name"}`` and nothing else: no email, phone, role, branch, job title,
staff number or pay. ``id`` is the person's account, the same id an expense
claim's claimant, a payroll roster row's staff member and an approval stage's
approver are stored under, so a picked id is always one those endpoints accept.

It exists because the full directory (``/v1/i/me/staff/``) needs
``school.teachers.view``, and the people who pick staff mostly hold no such key.
Ngozi, Lagoon View's finance admin, records Tolu Fashola's claim for lab
chemicals: she may raise claims and set up payroll, and is not meant to read
Tolu's phone number, date of birth or employment history. This answers the one
question her pickers ask, which name belongs to which person, and no other.

**The gate is the keys of the pickers it serves, and only those**
(:data:`PICKER_KEYS`): raising an expense claim, adding or correcting a payroll
roster row, editing an approval route's stages, creating or changing an
approver group or a dynamic role, and assigning or replacing a request's
approvers. A caller holding none of them is refused (403), which is every
parent, every pupil and every member of staff whose role picks nobody.

**Field Access: a transport-level directory.** A person's name beside an id is
how every picker and every approval trail names somebody, as the actor on a
log or the line manager on a record is named; it is not the staff profile.
The ``school.teachers`` name switches govern the staff screens and their
exports, not this list, so none is applied here, and no other registered
field of ``school.teachers`` reaches it under any name: the payload is built
by hand from two attributes, never through a serializer that could grow a key.
A search matches the name parts only, never the email or the staff number, so
nobody can be found by a value the list does not show.

"Active" is both halves: still employed (``employment_status`` Active, which
an approved leave leaves alone) and an account that is not suspended, so every
row is somebody who can be reimbursed and can act on an approval.

Scoped to ``request.tenant`` (the school the address names), never to a
parameter, so another school's staff cannot be listed. Not narrowed by branch:
an approver group or a claim at Lekki may name somebody posted at Ikeja.
Capped at :data:`StaffNamesView.LIMIT` rows, ordered by name; a type-ahead
sends ``q`` to narrow it.
"""
from __future__ import annotations

from django.db.models import F, Value
from django.db.models.functions import Concat, Trim
from rest_framework.views import APIView

from core.response import success_response
from core.search import search as core_search

from ..constants import EmploymentStatus
from .base import StaffViewMixin

#: Every key that drives a staff picker, each named by the endpoint that
#: stores the picked person.
PICKER_KEYS = [
    "finance.expenseclaim.create",   # an expense claim's claimant
    "finance.salary.create",         # a payroll roster row's staff member
    "finance.salary.update",
    "workflow.template.update",      # a person on an approval route's stage
    "workflow.group.create",         # an approver group's members, a dynamic role
    "workflow.group.update",
    "workflow.approvers.assign",     # a request's approvers, a replacement
]

#: What a typed query is matched against: the parts of the name the list shows.
NAME_FIELDS = ("user__first_name", "middle_name", "user__last_name")


class StaffNamesView(StaffViewMixin, APIView):
    """GET /v1/i/me/staff/names/?q= - the school's active staff, by id and name only.

    docstring-name: Staff names for pickers
    """

    rbac_permission = PICKER_KEYS
    pagination_class = None

    #: The most rows one answer carries. Enough for a whole school's staff in
    #: one picker, and a bound on a list nobody scrolls.
    LIMIT = 500

    def get(self, request):
        from vs_user.models import User

        from ..models import StaffProfile

        staff = StaffProfile.objects.filter(
            tenant=self.tenant, user__tenant=self.tenant,
            employment_status=EmploymentStatus.ACTIVE, user__status=User.Status.ACTIVE,
        )
        then = ("user__first_name", "user__last_name", "user_id")
        query = (request.query_params.get("q") or "").strip()
        staff = core_search(staff, query, fields=NAME_FIELDS, then=then) if query \
            else staff.order_by(*then)
        rows = staff.annotate(
            display_name=Trim(Concat(F("user__first_name"), Value(" "), F("user__last_name"))),
        ).values_list("user_id", "display_name")[: self.LIMIT]
        return success_response(
            "Staff names retrieved.",
            data=[{"id": user_id, "name": name} for user_id, name in rows],
        )
