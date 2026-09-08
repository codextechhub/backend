"""Where people are based, and the roster that reads a branch three ways.

FRD M12 v2.1, FR-010 and FR-019.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_ROLES_ASSIGN, PERM_UPDATE, PERM_VIEW
from ..serializers import BulkPostingSerializer, BulkRoleSerializer, StaffListSerializer
from ..exceptions import StaffHasLeft
from ..services import posting, roles
from .base import StaffViewMixin


class StaffBulkPostingView(StaffViewMixin, APIView):
    """POST /v1/i/me/staff/posting/ - move several people at once.

    **A posting change never touches a role grant**, and the response says so as
    well as the confirmation. A bulk action that quietly re-pinned grants would
    change what people may do while claiming to change where they sit.

    **Refuses to move somebody who has left.** They are still on the roster,
    because dropping them would hide that they were ever at the branch, and the
    screen draws them as finished and will not tick them - but a greyed row is a
    courtesy and this refusal is what makes it true.

    Warns and never refuses where somebody teaches at the branch they are
    leaving: a school moving a teacher mid-term is doing it deliberately, and a
    refusal would leave them cancelling assignments to make a move the system
    should not be arguing with.

    docstring-name: Move staff between branches
    """

    rbac_permission = PERM_UPDATE
    pending_tenant_surface = True

    @transaction.atomic
    def post(self, request):
        payload = BulkPostingSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        branch = posting.resolve_posting(self.tenant, data.get("branch"))
        # Resolved and narrowed before anything is written, so a partial bulk
        # never happens: a caller naming somebody they cannot see gets one 404
        # and no one is moved.
        people = self._resolve(data["staff_ids"])

        warnings = []
        for person in people:
            stranded = posting.set_posting(
                person, branch, actor=request.user, reason=data.get("reason", ""),
            )
            if stranded:
                warnings.append({
                    "code": "ASSIGNMENTS_LEFT_BEHIND",
                    "staff_id": person.pk,
                    "message": (
                        f"{person.user.first_name} {person.user.last_name} still "
                        f"teaches {', '.join(stranded)}. Moving them does not "
                        f"cancel those assignments."
                    ),
                    "classes": stranded,
                })

        return success_response(
            message=(
                f"{len(people)} moved. This did not change which branches their "
                f"roles reach."
            ),
            data={
                "moved": len(people),
                "role_grants_touched": False,
                "warnings": warnings,
            },
        )

    def _resolve(self, staff_ids):
        scoped = self.scoped_staff().filter(pk__in=staff_ids)
        found = {row.pk: row for row in scoped}
        missing = [pk for pk in staff_ids if pk not in found]
        if missing:
            raise NotFound("No such person at this school.")

        people = [found[pk] for pk in staff_ids]
        # Named rather than refused blankly, because the caller ticked rows and
        # is owed which of them stopped the move.
        gone = [person for person in people if not person.is_on_roll]
        if gone:
            raise StaffHasLeft(
                f"{', '.join(_name(person) for person in gone)} no longer "
                f"{'work' if len(gone) > 1 else 'works'} here, so their posting "
                f"cannot be moved. Nobody was moved."
            )
        return people


class StaffRosterView(StaffViewMixin, APIView):
    """GET /v1/i/me/staff/roster/?branch= - who works at one branch, and why.

    Three labelled groups, never one flat list. Posted here, reaching here
    through a role, and school-wide are three different facts, and a single list
    would tell an Ikeja administrator that the registrar is theirs. She is not
    at Ikeja; she is at the school, and she appears in every branch's roster.

    Posted here and school-wide are both movable: one changes which branch
    somebody is based at, the other gives a base to somebody who has none, and
    both are the same act through the same drawer. Reaching here is not, because
    those people are carried by a role rather than by a posting, so their row
    names the role and opening it goes to where that role is changed.

    People who have left stay on it and carry ``on_roll: false``. Dropping them
    would hide from the only screen that answers who is at a branch that
    somebody ever was; drawing them as finished, and refusing to move them,
    says both things at once.

    docstring-name: A branch's staff roster
    """

    rbac_permission = PERM_VIEW
    pending_tenant_surface = True

    def get(self, request):
        if not self.multi_branch:
            # One branch: the dimension recedes entirely rather than showing a
            # roster with one group and every person in it.
            raise NotFound("This school has one branch, so there is no roster.")

        branch = posting.resolve_posting(
            self.tenant, request.query_params.get("branch"),
        )
        if branch is None:
            raise NotFound("Say which branch.")

        posted, reaching, school_wide, via = posting.roster(
            self.tenant, request.user, branch,
        )
        context = self.serializer_context()

        # The role that carries somebody here rides on their row rather than in
        # the group's note, because it differs per person: Mr. Sule reaches
        # Ikeja as its Head Teacher and Mrs. Eze reaches it school-wide, and one
        # sentence over both would have to name neither.
        reaching_rows = StaffListSerializer(
            reaching, many=True, context=context,
        ).data
        for row in reaching_rows:
            row["via_roles"] = via.get(row["id"], [])
        return success_response(data={
            "branch": {"id": branch.pk, "name": branch.name},
            "total": len(posted) + len(reaching) + len(school_wide),
            "groups": [
                {
                    "key": "posted_here",
                    "title": "Posted here",
                    "note": (
                        f"Based at {branch.name}. Tick anybody here to move them "
                        f"to another branch."
                    ),
                    "movable": True,
                    "change_it": "",
                    "rows": StaffListSerializer(posted, many=True, context=context).data,
                },
                {
                    "key": "reaching_here",
                    "title": "Reaching here through a role",
                    "note": (
                        f"Based at another branch, and carried here by a role "
                        f"rather than by a posting. Each row names the role and "
                        f"says whether it is pinned to {branch.name} or is a "
                        f"school-wide one that reaches every branch. Open a row "
                        f"to change it."
                    ),
                    "movable": False,
                    "change_it": "Open a row to see the role",
                    "rows": reaching_rows,
                },
                {
                    "key": "school_wide",
                    "title": "School-wide",
                    "note": (
                        "No single base, so they belong to the school and appear "
                        "on every branch's roster. Tick anybody here to give "
                        "them one branch instead."
                    ),
                    "movable": True,
                    "change_it": "",
                    "rows": StaffListSerializer(
                        school_wide, many=True, context=context,
                    ).data,
                },
            ],
        })


class StaffBulkRoleView(StaffViewMixin, APIView):
    """POST /v1/i/me/staff/roles/bulk/ - one role, one reach, several people.

    Adds nothing to the RBAC model: it calls the same rows a single grant writes,
    inside one transaction, under RBAC's own key. What it adds is that the whole
    selection is resolved and narrowed before anything is written, so a partial
    bulk never happens, and that anybody who already holds the role is reported
    by name rather than granted twice.

    docstring-name: Grant one role to several staff
    """

    rbac_permission = PERM_ROLES_ASSIGN
    pending_tenant_surface = True

    @transaction.atomic
    def post(self, request):
        from .directory import ONBOARDING_ROLE_KEYS

        payload = BulkRoleSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        role = roles.resolve_role(
            self.tenant, data["role"],
            onboarding_keys=ONBOARDING_ROLE_KEYS if self.onboarding else None,
        )
        branch = posting.resolve_posting(self.tenant, data.get("branch"))

        scoped = self.scoped_staff().filter(pk__in=data["staff_ids"])
        found = {row.pk: row for row in scoped}
        if [pk for pk in data["staff_ids"] if pk not in found]:
            raise NotFound("No such person at this school.")

        granted, already = roles.grant_to_many(
            tenant=self.tenant, role=role, branch=branch,
            people=[found[pk] for pk in data["staff_ids"]], actor=request.user,
        )

        def named(rows):
            return [
                {
                    "id": row.pk,
                    "name": f"{row.user.first_name} {row.user.last_name}".strip(),
                }
                for row in rows
            ]

        return success_response(
            message=(
                f"{role.name} granted to {len(granted)}. Existing roles are "
                f"untouched."
            ),
            data={
                "role": {"id": role.pk, "key": role.key, "name": role.name},
                "reach": branch.name if branch else "School-wide",
                "granted": named(granted),
                # Named rather than counted: "2 already had it" sends somebody
                # back through a list of forty to work out which two.
                "already_held": named(already),
            },
        )


def _name(person) -> str:
    return f"{person.user.first_name} {person.user.last_name}".strip()
