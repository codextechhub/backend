"""Changing who approves a request while it waits, and delegations reaching waiting requests.

Greenfield has two branches, Ikeja and Lekki; Harbour has one. Every request
here is raised at Greenfield's Ikeja branch by Rita unless a test says
otherwise, and runs a two-stage ladder: Funke approves the first stage and
Tunde the second, each through an approver group.
"""
from __future__ import annotations

import datetime as dt
import itertools
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    make_assignment, make_branch, make_permission, make_role,
    make_role_permission, make_school, make_school_admin,
)
from vs_workflow.constants import (
    NOTIF_EVENT_APPROVER_REMOVED, NOTIF_EVENT_STAGE_ACTIVATED,
    PERM_APPROVERS_ASSIGN, PERM_INSTANCE_VIEW,
    ApproverChangeKind, ApproverSource, AuditEventType, StageAdvanceRule,
    StageKind, WorkflowInstanceStatus, WorkflowStageStatus,
    WorkflowStageAction as Vote,
)
from vs_workflow.exceptions import (
    ApproverAlreadyVotedError, ApproverConflictError, ApproverOutOfReachError,
    InstanceNotOpenError, NotAnEligibleApproverError, ReasonRequiredError,
    StageEmptyError, StageNotChangeableError,
)
from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.handlers.registry import _REGISTRY
from vs_workflow.models import (
    ApprovalDelegation, WorkflowApproverChange, WorkflowApproverGroup,
    WorkflowApproverGroupMember, WorkflowAuditLog, WorkflowInstance, WorkflowStage,
    WorkflowStageApprover, WorkflowStageAssignment, WorkflowStageInstance,
    WorkflowTemplate,
)
from vs_workflow.services import actions, reassignment, routing

DOC = "tests.reassign_request"
BASE = "/v1/workflow"
_numbers = itertools.count(1)


class _Handler(BaseWorkflowHandler):
    noun = "Test request"
    approval_releases_nothing = True

    def resolve_default_template_code(self, document):
        return "default"


def _person(tenant, email, first, last, branch=None):
    user = make_school_admin(None, email=email, tenant=tenant)
    user.first_name, user.last_name, user.branch = first, last, branch
    user.save()
    return user


def _grant(user, keys, *, branch=None):
    role = make_role(user.tenant, name=f"Reassign grant {next(_numbers)}")
    for key in keys:
        make_role_permission(role, make_permission(key))
    make_assignment(user.tenant, user, role, branch=branch)


def _ladder(tenant, first_group, second_group):
    template = WorkflowTemplate.objects.create(
        tenant=tenant, document_type=DOC, code="default", name="Test ladder",
    )
    stages = [
        WorkflowStage.objects.create(
            template=template, code=code, label=label, kind=StageKind.APPROVAL,
            order=order, approver_source=ApproverSource.WORKFLOW_GROUP,
            approver_group=group, advance_rule=StageAdvanceRule.ANY,
        )
        for order, (code, label, group) in enumerate(
            [("hod", "Head of Department", first_group),
             ("bursar", "Bursar", second_group)], start=1)
    ]
    return template, stages


def _group(tenant, code, *members):
    group = WorkflowApproverGroup.objects.create(tenant=tenant, code=code, name=code)
    for user in members:
        WorkflowApproverGroupMember.objects.create(group=group, kind="USER", user=user)
    return group


class _Fixture(TestCase):
    @classmethod
    def setUpClass(cls):
        registry = patch.dict(_REGISTRY, {DOC: _Handler()})
        registry.start()
        cls.addClassCleanup(registry.stop)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="reassign-greenfield", name="Greenfield")
        cls.tenant = cls.school.tenant
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.lekki = make_branch(cls.school, name="Lekki Branch", is_main=False)
        t = cls.tenant

        cls.admin = _person(t, "admin@green.test", "Ada", "Obi")
        _grant(cls.admin, [PERM_INSTANCE_VIEW, PERM_APPROVERS_ASSIGN])
        cls.ikeja_admin = _person(t, "ikeja-admin@green.test", "Ike", "Admin", cls.ikeja)
        _grant(cls.ikeja_admin, [PERM_INSTANCE_VIEW, PERM_APPROVERS_ASSIGN], branch=cls.ikeja)
        cls.viewer = _person(t, "viewer@green.test", "Vera", "Viewer")
        _grant(cls.viewer, [PERM_INSTANCE_VIEW])

        cls.rita = _person(t, "rita@green.test", "Rita", "Requester", cls.ikeja)
        cls.funke = _person(t, "funke@green.test", "Funke", "Adeyemi", cls.ikeja)
        cls.tunde = _person(t, "tunde@green.test", "Tunde", "Bello", cls.ikeja)
        cls.okafor = _person(t, "okafor@green.test", "Chidi", "Okafor")
        cls.bola = _person(t, "bola@green.test", "Bola", "Lekki", cls.lekki)

        cls.template, (cls.hod, cls.bursar) = _ladder(
            t, _group(t, "hod", cls.funke), _group(t, "bursar", cls.tunde),
        )

        cls.other = make_school(slug="reassign-other", name="Elsewhere")
        cls.other_branch = make_branch(cls.other, name="Main")
        cls.stranger = _person(cls.other.tenant, "stranger@else.test", "Sam", "Stranger",
                               cls.other_branch)
        cls.other_template, _ = _ladder(
            cls.other.tenant, _group(cls.other.tenant, "hod", cls.stranger),
            _group(cls.other.tenant, "bursar", cls.stranger),
        )

    # ── builders ─────────────────────────────────────────────────────────

    @classmethod
    def raise_request(cls, *, branch=None, requester=None, title="Books for JSS1",
                      tenant=None, template=None):
        instance = WorkflowInstance.all_objects.create(
            tenant=tenant or cls.tenant, branch=branch or cls.ikeja,
            template=template or cls.template,
            document_content_type=ContentType.objects.get_for_model(WorkflowTemplate),
            document_object_id=f"doc-{next(_numbers)}", document_type=DOC,
            status=WorkflowInstanceStatus.SUBMITTED,
            requested_by=requester or cls.rita, submitted_at=timezone.now(),
            document_summary={"title": title},
        )
        routing.advance_instance(instance, current_attempt=1)
        return instance

    def seats(self, instance, stage=None):
        instance.refresh_from_db()
        si = (WorkflowStageInstance.objects
              .filter(instance=instance, stage=stage or instance.current_stage)
              .order_by("-attempt").first())
        return {
            (row.user_id, row.on_behalf_of_id)
            for row in WorkflowStageApprover.objects.filter(stage_instance=si, attempt=si.attempt)
        }

    def set(self, instance, stage, *people, reason="Funke is on leave", actor=None):
        return reassignment.set_stage_approvers(
            instance.pk, stage_id=stage.pk, user_ids=[p.pk for p in people],
            reason=reason, actor=actor or self.admin,
        )

    def unanimous(self, stage):
        stage.advance_rule = StageAdvanceRule.UNANIMOUS
        stage.save(update_fields=["advance_rule"])

    def notices(self):
        """Patch the notification hand-off; returns the mock whose calls say who was told."""
        patcher = patch("vs_workflow.tasks.dispatch_notification.delay")
        self.addCleanup(patcher.stop)
        return patcher.start()

    @staticmethod
    def told(dispatch, event_key):
        return {
            user_id for call in dispatch.call_args_list
            if call.kwargs["event_key"] == event_key
            for user_id in call.kwargs["recipient_user_ids"]
        }


# ── Security ─────────────────────────────────────────────────────────────────

class ApproversEndpointSecurityTests(_Fixture):
    def test_every_write_and_the_approvers_read_refuse_a_caller_without_the_key(self):
        request = self.raise_request()
        client = TenantAPIClient(self.viewer)
        body = {"stage": self.hod.pk, "approvers": [self.tunde.pk], "reason": "Away"}
        calls = [
            client.get(f"{BASE}/instances/{request.pk}/approvers/"),
            client.post(f"{BASE}/instances/{request.pk}/approvers/", body, format="json"),
            client.post(f"{BASE}/instances/{request.pk}/approvers/reset/",
                        {"stage": self.bursar.pk, "reason": "Away"}, format="json"),
            client.post(f"{BASE}/instances/replace-approver/",
                        {"from_user": self.funke.pk, "to_user": self.tunde.pk,
                         "instance_ids": [request.pk], "reason": "Away"}, format="json"),
        ]
        self.assertEqual([c.status_code for c in calls], [403, 403, 403, 403])
        self.assertEqual(self.seats(request), {(self.funke.pk, None)})

    def test_another_tenants_request_is_not_found(self):
        foreign = self.raise_request(tenant=self.other.tenant, template=self.other_template,
                                     branch=self.other_branch, requester=self.stranger)
        client = TenantAPIClient(self.admin)
        read = client.get(f"{BASE}/instances/{foreign.pk}/approvers/")
        write = client.post(f"{BASE}/instances/{foreign.pk}/approvers/",
                            {"stage": foreign.current_stage_id, "approvers": [self.admin.pk],
                             "reason": "Mine now"}, format="json")
        self.assertEqual((read.status_code, write.status_code), (404, 404))

    def test_a_branch_administrator_reaches_only_their_own_branchs_requests(self):
        ikeja = self.raise_request(branch=self.ikeja)
        lekki = self.raise_request(branch=self.lekki)
        client = TenantAPIClient(self.ikeja_admin)
        self.assertEqual(client.get(f"{BASE}/instances/{ikeja.pk}/approvers/").status_code, 200)
        self.assertEqual(client.get(f"{BASE}/instances/{lekki.pk}/approvers/").status_code, 404)
        refused = client.post(f"{BASE}/instances/{lekki.pk}/approvers/",
                              {"stage": self.hod.pk, "approvers": [self.tunde.pk],
                               "reason": "Away"}, format="json")
        self.assertEqual(refused.status_code, 404)

    def test_a_bulk_replace_skips_a_request_out_of_reach_as_not_found(self):
        ikeja = self.raise_request(branch=self.ikeja)
        lekki = self.raise_request(branch=self.lekki)
        response = TenantAPIClient(self.ikeja_admin).post(
            f"{BASE}/instances/replace-approver/",
            {"from_user": self.funke.pk, "to_user": self.tunde.pk,
             "instance_ids": [ikeja.pk, lekki.pk], "reason": "Funke is away"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        results = {r["instance_id"]: r for r in response.data["data"]["results"]}
        self.assertEqual(results[ikeja.pk]["outcome"], "replaced")
        self.assertEqual(results[lekki.pk],
                         {"instance_id": lekki.pk, "outcome": "skipped",
                          "detail": "This request was not found."})
        self.assertEqual(self.seats(lekki), {(self.funke.pk, None)})


class OneBranchSchoolTests(TestCase):
    """At a school with one branch, branch reach recedes: everybody reaches it."""

    @classmethod
    def setUpClass(cls):
        registry = patch.dict(_REGISTRY, {DOC: _Handler()})
        registry.start()
        cls.addClassCleanup(registry.stop)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        school = make_school(slug="reassign-harbour", name="Harbour")
        cls.tenant = school.tenant
        cls.main = make_branch(school, name="Main")
        cls.head = _person(cls.tenant, "head@harbour.test", "Hauwa", "Head", cls.main)
        # Pinned to the only branch, which reads as the whole school.
        _grant(cls.head, [PERM_INSTANCE_VIEW, PERM_APPROVERS_ASSIGN], branch=cls.main)
        cls.rita = _person(cls.tenant, "rita@harbour.test", "Rita", "R", cls.main)
        cls.first = _person(cls.tenant, "first@harbour.test", "Femi", "First", cls.main)
        cls.cover = _person(cls.tenant, "cover@harbour.test", "Kemi", "Cover")
        cls.template, (cls.hod, _) = _ladder(
            cls.tenant, _group(cls.tenant, "hod", cls.first),
            _group(cls.tenant, "bursar", cls.first),
        )

    def test_the_head_moves_a_request_to_a_colleague_with_no_posting(self):
        instance = WorkflowInstance.all_objects.create(
            tenant=self.tenant, branch=self.main, template=self.template,
            document_content_type=ContentType.objects.get_for_model(WorkflowTemplate),
            document_object_id="harbour-1", document_type=DOC,
            status=WorkflowInstanceStatus.SUBMITTED, requested_by=self.rita,
            submitted_at=timezone.now(),
        )
        routing.advance_instance(instance, current_attempt=1)
        client = TenantAPIClient(self.head)
        response = client.post(f"{BASE}/instances/{instance.pk}/approvers/",
                               {"stage": self.hod.pk, "approvers": [self.cover.pk],
                                "reason": "Femi is on a course"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        active = [s for s in response.data["data"]["stages"] if s["state"] == "ACTIVE"][0]
        self.assertEqual([a["id"] for a in active["approvers"]], [self.cover.pk])
        rows = client.get(f"{BASE}/instances/").data["data"]
        self.assertEqual([p["id"] for p in rows[0]["waiting_on"]], [self.cover.pk])


# ── The waiting stage ───────────────────────────────────────────────────────

class ActiveStageChangeTests(_Fixture):
    def test_a_swap_hands_the_decision_to_the_new_person_and_tells_both(self):
        request = self.raise_request()
        dispatch = self.notices()
        with self.captureOnCommitCallbacks(execute=True):
            self.set(request, self.hod, self.tunde)

        self.assertEqual(self.seats(request), {(self.tunde.pk, None)})
        self.assertEqual(self.told(dispatch, NOTIF_EVENT_STAGE_ACTIVATED), {str(self.tunde.pk)})
        self.assertEqual(self.told(dispatch, NOTIF_EVENT_APPROVER_REMOVED), {str(self.funke.pk)})
        with self.assertRaises(NotAnEligibleApproverError):
            actions.record_action(request.pk, self.funke, Vote.APPROVED)
        actions.record_action(request.pk, self.tunde, Vote.APPROVED)
        request.refresh_from_db()
        self.assertEqual(request.current_stage, self.bursar)

        change = WorkflowApproverChange.objects.get(instance=request)
        self.assertEqual(
            (change.kind, change.removed_user, change.added_user, change.reason, change.changed_by),
            (ApproverChangeKind.ACTIVE_STAGE, self.funke, self.tunde, "Funke is on leave", self.admin),
        )
        audit = WorkflowAuditLog.objects.get(instance=request,
                                             event_type=AuditEventType.APPROVERS_CHANGED)
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.context["reason"], "Funke is on leave")

    def test_somebody_who_has_already_approved_cannot_be_removed(self):
        self.unanimous(self.hod)
        request = self.raise_request()
        self.set(request, self.hod, self.funke, self.tunde)
        actions.record_action(request.pk, self.funke, Vote.APPROVED)
        with self.assertRaises(ApproverAlreadyVotedError) as caught:
            self.set(request, self.hod, self.tunde, self.okafor)
        self.assertIn("Funke Adeyemi has already approved", caught.exception.message)
        self.assertEqual(self.seats(request), {(self.funke.pk, None), (self.tunde.pk, None)})

    def test_the_requester_cannot_be_made_an_approver(self):
        request = self.raise_request()
        with self.assertRaises(ApproverConflictError) as caught:
            self.set(request, self.hod, self.funke, self.rita)
        self.assertEqual(caught.exception.message,
                         "Rita Requester cannot approve this request, because they raised it.")

    def test_somebody_outside_the_requests_branch_or_tenant_cannot_be_added(self):
        request = self.raise_request(branch=self.ikeja)
        with self.assertRaises(ApproverOutOfReachError) as caught:
            self.set(request, self.hod, self.bola)
        self.assertIn("does not work in Ikeja Branch", caught.exception.message)
        with self.assertRaises(ApproverOutOfReachError) as caught:
            self.set(request, self.hod, self.stranger)
        # Somebody in another tenant is not named: that would confirm they exist.
        self.assertNotIn("Stranger", caught.exception.message)
        self.okafor.status = "DEACTIVATED"
        self.okafor.save()
        with self.assertRaises(ApproverOutOfReachError):
            self.set(request, self.hod, self.okafor)
        self.assertEqual(self.seats(request), {(self.funke.pk, None)})

    def test_somebody_across_the_whole_tenant_reaches_every_branch(self):
        request = self.raise_request(branch=self.lekki)
        self.set(request, self.hod, self.okafor)
        self.assertEqual(self.seats(request), {(self.okafor.pk, None)})

    def test_a_stage_cannot_be_left_with_nobody_or_below_its_quorum(self):
        request = self.raise_request()
        with self.assertRaises(StageEmptyError):
            self.set(request, self.hod)
        self.hod.advance_rule, self.hod.quorum_count = StageAdvanceRule.QUORUM, 2
        self.hod.save(update_fields=["advance_rule", "quorum_count"])
        with self.assertRaises(StageEmptyError) as caught:
            self.set(request, self.hod, self.tunde)
        self.assertIn("needs 2 approvals", caught.exception.message)

    def test_removing_the_last_person_to_decide_completes_an_everyone_must_approve_stage(self):
        self.unanimous(self.hod)
        request = self.raise_request()
        self.set(request, self.hod, self.funke, self.tunde)
        actions.record_action(request.pk, self.funke, Vote.APPROVED)
        request.refresh_from_db()
        self.assertEqual(request.current_stage, self.hod)

        self.set(request, self.hod, self.funke, reason="Tunde has left")

        request.refresh_from_db()
        self.assertEqual(request.current_stage, self.bursar)
        self.assertEqual(self.seats(request), {(self.tunde.pk, None)})
        self.assertEqual(
            WorkflowStageInstance.objects.get(instance=request, stage=self.hod).status,
            WorkflowStageStatus.APPROVED,
        )

    def test_a_reason_is_required_and_kept_short(self):
        request = self.raise_request()
        for reason in ("", "   ", "x" * 501):
            with self.assertRaises(ReasonRequiredError):
                self.set(request, self.hod, self.tunde, reason=reason)

    def test_a_finished_request_and_a_decided_stage_cannot_change(self):
        request = self.raise_request()
        actions.record_action(request.pk, self.funke, Vote.APPROVED)
        with self.assertRaises(StageNotChangeableError):
            self.set(request, self.hod, self.okafor)
        actions.cancel(request.pk, self.admin, "Duplicate")
        with self.assertRaises(InstanceNotOpenError):
            self.set(request, self.bursar, self.okafor)

    def test_a_new_approvers_exclusive_delegate_takes_their_place(self):
        request = self.raise_request()
        now = timezone.now()
        ApprovalDelegation.objects.create(
            tenant=self.tenant, delegator=self.okafor, delegate=self.tunde,
            starts_at=now - dt.timedelta(hours=1), ends_at=now + dt.timedelta(days=3),
            exclusive=True, applied_at=now,
        )
        self.set(request, self.hod, self.okafor)
        self.assertEqual(self.seats(request), {(self.tunde.pk, self.okafor.pk)})


# ── Stages still to come ────────────────────────────────────────────────────

class UpcomingStageTests(_Fixture):
    def test_a_stage_chosen_in_advance_opens_with_that_choice_every_time(self):
        request = self.raise_request()
        self.set(request, self.bursar, self.okafor, reason="Tunde is at a conference")
        self.assertEqual(self.seats(request), {(self.funke.pk, None)})

        actions.record_action(request.pk, self.funke, Vote.APPROVED)
        self.assertEqual(self.seats(request), {(self.okafor.pk, None)})

        actions.record_action(request.pk, self.okafor, Vote.RETURNED, comment="Attach a quote")
        actions.resubmit(request.pk, self.rita)
        request.refresh_from_db()
        self.assertEqual(request.current_stage, self.bursar)
        self.assertEqual(self.seats(request), {(self.okafor.pk, None)})
        change = WorkflowApproverChange.objects.get(instance=request)
        self.assertEqual((change.kind, change.removed_user, change.added_user),
                         (ApproverChangeKind.UPCOMING_ASSIGNMENT, self.tunde, self.okafor))

    def test_the_stage_that_returned_a_request_can_be_chosen_before_it_reopens(self):
        request = self.raise_request()
        actions.record_action(request.pk, self.funke, Vote.RETURNED, comment="Wrong form")
        self.set(request, self.hod, self.tunde)
        actions.resubmit(request.pk, self.rita)
        self.assertEqual(self.seats(request), {(self.tunde.pk, None)})

    def test_reset_lets_the_stage_resolve_its_approvers_normally(self):
        request = self.raise_request()
        self.set(request, self.bursar, self.okafor)
        reassignment.reset_stage_assignment(
            request.pk, stage_id=self.bursar.pk, reason="Tunde is back", actor=self.admin,
        )
        self.assertFalse(WorkflowStageAssignment.objects.filter(instance=request).exists())
        actions.record_action(request.pk, self.funke, Vote.APPROVED)
        self.assertEqual(self.seats(request), {(self.tunde.pk, None)})
        reset = WorkflowApproverChange.objects.get(
            instance=request, kind=ApproverChangeKind.ASSIGNMENT_RESET)
        self.assertEqual((reset.removed_user, reset.added_user), (self.okafor, None))

    def test_reset_refuses_a_stage_nobody_chose_or_one_already_open(self):
        request = self.raise_request()
        for stage in (self.bursar, self.hod):
            with self.assertRaises(StageNotChangeableError):
                reassignment.reset_stage_assignment(
                    request.pk, stage_id=stage.pk, reason="Tidy", actor=self.admin)

    def test_the_parking_repair_counts_a_choice_as_somebody_to_approve(self):
        from vs_workflow.services.parking import ResolutionCache

        request = self.raise_request()
        self.set(request, self.bursar, self.okafor)
        WorkflowApproverGroupMember.objects.filter(group=self.bursar.approver_group).delete()
        self.assertTrue(ResolutionCache().has_candidates(self.bursar, request))


# ── What the screen reads ───────────────────────────────────────────────────

class ApproversOverviewTests(_Fixture):
    def test_the_body_lists_each_stage_with_its_people_and_the_history(self):
        request = self.raise_request()
        self.set(request, self.bursar, self.okafor, reason="Tunde is away")
        response = TenantAPIClient(self.admin).get(f"{BASE}/instances/{request.pk}/approvers/")
        self.assertEqual(response.status_code, 200, response.data)
        body = response.data["data"]
        self.assertEqual((body["instance_id"], body["may_change"], body["blocked_reason"]),
                         (request.pk, True, None))
        active, upcoming = body["stages"]
        self.assertEqual(
            {k: active[k] for k in ("stage_id", "label", "state", "advance_rule", "preview",
                                    "assignment", "may_change", "quorum_count")},
            {"stage_id": self.hod.pk, "label": "Head of Department", "state": "ACTIVE",
             "advance_rule": "ANY", "preview": False, "assignment": None,
             "may_change": True, "quorum_count": None},
        )
        self.assertEqual(active["approvers"], [
            {"id": self.funke.pk, "name": "Funke Adeyemi", "on_behalf_of": None, "vote": None},
        ])
        self.assertEqual((upcoming["state"], upcoming["preview"]), ("UPCOMING", False))
        self.assertEqual(upcoming["assignment"]["reason"], "Tunde is away")
        self.assertEqual(upcoming["assignment"]["set_by"], {"id": self.admin.pk, "name": "Ada Obi"})
        self.assertEqual([a["id"] for a in upcoming["approvers"]], [self.okafor.pk])
        self.assertEqual(body["history"][0]["removed"], {"id": self.tunde.pk, "name": "Tunde Bello"})
        self.assertEqual(body["history"][0]["added"], {"id": self.okafor.pk, "name": "Chidi Okafor"})
        self.assertEqual(body["history"][0]["stage_label"], "Bursar")

    def test_a_stage_nobody_chose_previews_who_would_approve_it(self):
        request = self.raise_request()
        body = reassignment.approver_overview(request)
        self.assertTrue(body["stages"][1]["preview"])
        self.assertEqual([a["id"] for a in body["stages"][1]["approvers"]], [self.tunde.pk])

    def test_decided_stages_show_their_votes_and_a_finished_request_is_locked(self):
        request = self.raise_request()
        actions.record_action(request.pk, self.funke, Vote.APPROVED)
        actions.record_action(request.pk, self.tunde, Vote.APPROVED)
        request.refresh_from_db()
        body = reassignment.approver_overview(request)
        self.assertFalse(body["may_change"])
        self.assertIn("approved", body["blocked_reason"])
        self.assertEqual([s["state"] for s in body["stages"]], ["DONE", "DONE"])
        self.assertEqual(body["stages"][0]["approvers"][0]["vote"], "APPROVED")
        self.assertFalse(any(s["may_change"] for s in body["stages"]))

    def test_a_refusal_answers_with_its_code_and_a_plain_message(self):
        request = self.raise_request()
        response = TenantAPIClient(self.admin).post(
            f"{BASE}/instances/{request.pk}/approvers/",
            {"stage": self.hod.pk, "approvers": [self.rita.pk], "reason": "Cover"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "APPROVER_CONFLICT")
        self.assertIn("Rita Requester", response.data["message"])
        blank = TenantAPIClient(self.admin).post(
            f"{BASE}/instances/{request.pk}/approvers/",
            {"stage": self.hod.pk, "approvers": [self.tunde.pk]}, format="json",
        )
        self.assertEqual(blank.data["error"]["code"], "REASON_REQUIRED")


# ── Many requests at once ───────────────────────────────────────────────────

class ReplaceApproverTests(_Fixture):
    def test_each_request_is_replaced_or_skipped_on_its_own(self):
        self.unanimous(self.hod)
        waiting = self.raise_request(title="Waiting on Funke")
        decided = self.raise_request(title="Funke has approved")
        self.set(decided, self.hod, self.funke, self.tunde)
        actions.record_action(decided.pk, self.funke, Vote.APPROVED)
        ahead = self.raise_request(title="Funke chosen for later")
        self.set(ahead, self.hod, self.tunde)
        self.set(ahead, self.bursar, self.funke)
        foreign = self.raise_request(tenant=self.other.tenant, template=self.other_template,
                                     branch=self.other_branch, requester=self.stranger)

        response = TenantAPIClient(self.admin).post(
            f"{BASE}/instances/replace-approver/",
            {"from_user": self.funke.pk, "to_user": self.okafor.pk,
             "instance_ids": [waiting.pk, decided.pk, ahead.pk, foreign.pk],
             "reason": "Funke is on leave"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        body = response.data["data"]
        self.assertEqual((body["replaced"], body["skipped"]), (2, 2))
        self.assertEqual([r["outcome"] for r in body["results"]],
                         ["replaced", "skipped", "replaced", "skipped"])
        self.assertIn("already approved", body["results"][1]["detail"])
        self.assertEqual(self.seats(waiting), {(self.okafor.pk, None)})
        self.assertEqual(self.seats(decided), {(self.funke.pk, None), (self.tunde.pk, None)})
        self.assertEqual(
            set(WorkflowStageAssignment.objects.get(instance=ahead, stage=self.bursar)
                .approvers.values_list("pk", flat=True)),
            {self.okafor.pk},
        )
        self.assertTrue(WorkflowApproverChange.objects.filter(
            instance=waiting, kind=ApproverChangeKind.BULK_REPLACE,
            removed_user=self.funke, added_user=self.okafor).exists())

    def test_the_new_person_must_be_able_to_approve_here_at_all(self):
        request = self.raise_request()
        response = TenantAPIClient(self.admin).post(
            f"{BASE}/instances/replace-approver/",
            {"from_user": self.funke.pk, "to_user": self.stranger.pk,
             "instance_ids": [request.pk], "reason": "Away"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "APPROVER_OUT_OF_REACH")


# ── The administrators' list ────────────────────────────────────────────────

class InstanceListTests(_Fixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        lagos = dt.timezone(dt.timedelta(hours=1))
        cls.r1 = cls.raise_request(title="Books for JSS1")
        cls.r2 = cls.raise_request(branch=cls.lekki, title="Lab stools")
        actions.record_action(cls.r2.pk, cls.funke, Vote.APPROVED)
        cls.r3 = cls.raise_request(requester=cls.okafor, title="Sports day")
        actions.cancel(cls.r3.pk, cls.admin, "Duplicate")
        WorkflowInstance.all_objects.filter(pk=cls.r3.pk).update(request_for=cls.bola)
        for request, day in ((cls.r1, 1), (cls.r2, 10), (cls.r3, 20)):
            WorkflowInstance.all_objects.filter(pk=request.pk).update(
                submitted_at=dt.datetime(2026, 9, day, 10, tzinfo=lagos))
        WorkflowStageInstance.objects.filter(instance=cls.r1).update(
            activated_at=timezone.now() - dt.timedelta(days=5))

    def ids(self, **params):
        response = TenantAPIClient(self.admin).get(f"{BASE}/instances/", params)
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["data"]}

    def test_each_filter_alone(self):
        r1, r2, r3 = self.r1.pk, self.r2.pk, self.r3.pk
        cases = [
            ({}, {r1, r2, r3}),
            ({"document_type": DOC}, {r1, r2, r3}),
            ({"status": "IN_PROGRESS"}, {r1, r2}),
            ({"status": "IN_PROGRESS,CANCELLED"}, {r1, r2, r3}),
            ({"requested_by": self.okafor.pk}, {r3}),
            ({"request_for": self.bola.pk}, {r3}),
            ({"waiting_on": self.funke.pk}, {r1}),
            ({"waiting_on": self.tunde.pk}, {r2}),
            ({"stage": self.bursar.pk}, {r2}),
            ({"waiting_longer_than": 3}, {r1}),
            ({"submitted_from": "2026-09-10"}, {r2, r3}),
            ({"submitted_to": "2026-09-10"}, {r1, r2}),
            ({"branch": self.lekki.pk}, {r2}),
            ({"search": "lab"}, {r2}),
            ({"search": r1}, {r1}),
        ]
        for params, expected in cases:
            with self.subTest(**{k: str(v) for k, v in params.items()}):
                self.assertEqual(self.ids(**params), expected)

    def test_filters_combine(self):
        self.assertEqual(
            self.ids(status="IN_PROGRESS", branch=self.ikeja.pk, waiting_on=self.funke.pk),
            {self.r1.pk},
        )
        self.assertEqual(self.ids(status="IN_PROGRESS", branch=self.lekki.pk,
                                  waiting_on=self.funke.pk), set())

    def test_waiting_on_includes_a_delegate(self):
        si = WorkflowStageInstance.objects.get(instance=self.r1, status="ACTIVE")
        WorkflowStageApprover.objects.create(stage_instance=si, user=self.okafor,
                                             on_behalf_of=self.funke, attempt=si.attempt)
        self.assertEqual(self.ids(waiting_on=self.okafor.pk), {self.r1.pk})

    def test_an_empty_answer_is_an_empty_list(self):
        response = TenantAPIClient(self.admin).get(f"{BASE}/instances/",
                                                   {"document_type": "nothing.here"})
        self.assertEqual(response.data["data"], [])

    def test_an_unreadable_value_is_refused(self):
        client = TenantAPIClient(self.admin)
        for params in ({"waiting_longer_than": "soon"}, {"submitted_from": "yesterday"}):
            with self.subTest(**params):
                self.assertEqual(client.get(f"{BASE}/instances/", params).status_code, 400)

    def test_a_row_says_who_it_waits_on_and_where_it_stands(self):
        response = TenantAPIClient(self.admin).get(f"{BASE}/instances/")
        rows = {row["id"]: row for row in response.data["data"]}
        waiting, cancelled = rows[self.r1.pk], rows[self.r3.pk]
        self.assertEqual(waiting["waiting_on"], [
            {"id": self.funke.pk, "name": "Funke Adeyemi", "on_behalf_of": None}])
        self.assertIsNotNone(waiting["waiting_since"])
        self.assertEqual(waiting["stage_position"], {"index": 1, "total": 2})
        self.assertEqual(waiting["branch"], {"id": self.ikeja.pk, "name": "Ikeja Branch"})
        self.assertIsNone(waiting["request_for"])
        self.assertEqual(rows[self.r2.pk]["stage_position"], {"index": 2, "total": 2})
        self.assertEqual(cancelled["request_for"], {"id": self.bola.pk, "name": "Bola Lekki"})
        self.assertEqual((cancelled["waiting_on"], cancelled["waiting_since"]), ([], None))

    def test_a_page_costs_the_same_however_many_rows_it_holds(self):
        client = TenantAPIClient(self.admin)
        client.get(f"{BASE}/instances/")
        with CaptureQueriesContext(connection) as three:
            client.get(f"{BASE}/instances/")
        for _ in range(3):
            self.raise_request()
        with CaptureQueriesContext(connection) as six:
            response = client.get(f"{BASE}/instances/")
        self.assertEqual(len(response.data["data"]), 6)
        self.assertEqual(len(six), len(three))

    def test_the_personal_queues_keep_the_plain_row(self):
        response = TenantAPIClient(self.funke).get(f"{BASE}/dashboard/pending/")
        self.assertNotIn("waiting_on", response.data["results"][0])

    def test_filter_options_name_the_stages_of_a_document_type(self):
        client = TenantAPIClient(self.admin)
        response = client.get(f"{BASE}/instances/filter-options/", {"document_type": DOC})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["stages"], [
            {"id": self.hod.pk, "label": "Head of Department", "template_code": "default"},
            {"id": self.bursar.pk, "label": "Bursar", "template_code": "default"},
        ])
        empty = client.get(f"{BASE}/instances/filter-options/")
        self.assertEqual(empty.data["data"]["stages"], [])


# ── Delegations ─────────────────────────────────────────────────────────────

class DelegationReachTests(_Fixture):
    def body(self, *, start=-1, **overrides):
        now = timezone.now()
        body = {"delegate": self.tunde.pk,
                "starts_at": (now + dt.timedelta(hours=start)).isoformat(),
                "ends_at": (now + dt.timedelta(days=5)).isoformat(),
                "reason": "Annual leave"}
        body.update(overrides)
        return body

    def test_an_administrator_delegates_for_somebody_else_and_it_reaches_waiting_requests(self):
        request = self.raise_request()
        dispatch = self.notices()
        with self.captureOnCommitCallbacks(execute=True):
            response = TenantAPIClient(self.admin).post(
                f"{BASE}/delegations/", self.body(delegator=self.funke.pk), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["delegator"], response.data["applied_to_waiting"]),
                         (self.funke.pk, 1))
        self.assertEqual(response.data["created_by"], {"id": self.admin.pk, "name": "Ada Obi"})
        self.assertEqual(self.seats(request),
                         {(self.funke.pk, None), (self.tunde.pk, self.funke.pk)})
        self.assertEqual(self.told(dispatch, NOTIF_EVENT_STAGE_ACTIVATED), {str(self.tunde.pk)})
        actions.record_action(request.pk, self.tunde, Vote.APPROVED)
        request.refresh_from_db()
        self.assertEqual(request.current_stage, self.bursar)

    def test_only_an_administrator_with_the_key_may_delegate_for_somebody_else(self):
        response = TenantAPIClient(self.viewer).post(
            f"{BASE}/delegations/", self.body(delegator=self.funke.pk), format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ApprovalDelegation.objects.filter(delegator=self.funke).exists())

    def test_a_person_still_delegates_their_own_approvals(self):
        response = TenantAPIClient(self.funke).post(
            f"{BASE}/delegations/", self.body(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["delegator"], response.data["created_by"]["id"]),
                         (self.funke.pk, self.funke.pk))

    def test_a_delegation_starting_later_reaches_waiting_requests_when_it_starts(self):
        from vs_workflow.tasks import apply_started_delegations

        request = self.raise_request()
        response = TenantAPIClient(self.funke).post(
            f"{BASE}/delegations/", self.body(start=24), format="json")
        self.assertEqual(response.data["applied_to_waiting"], 0)
        self.assertEqual(self.seats(request), {(self.funke.pk, None)})

        delegation = ApprovalDelegation.objects.get(pk=response.data["id"])
        delegation.starts_at = timezone.now() - dt.timedelta(minutes=1)
        delegation.save(update_fields=["starts_at"])
        self.assertEqual(apply_started_delegations(), 1)
        self.assertEqual(self.seats(request),
                         {(self.funke.pk, None), (self.tunde.pk, self.funke.pk)})
        self.assertEqual(apply_started_delegations(), 0)

    def test_revoking_pulls_the_delegate_back_and_restores_an_exclusive_delegator(self):
        request = self.raise_request()
        created = TenantAPIClient(self.funke).post(
            f"{BASE}/delegations/", self.body(exclusive=True), format="json")
        self.assertEqual(self.seats(request), {(self.tunde.pk, self.funke.pk)})

        dispatch = self.notices()
        with self.captureOnCommitCallbacks(execute=True):
            revoked = TenantAPIClient(self.funke).post(
                f"{BASE}/delegations/{created.data['id']}/revoke/")
        self.assertEqual(revoked.status_code, 200, revoked.data)
        self.assertEqual(self.seats(request), {(self.funke.pk, None)})
        self.assertEqual(self.told(dispatch, NOTIF_EVENT_APPROVER_REMOVED), {str(self.tunde.pk)})
        self.assertEqual(self.told(dispatch, NOTIF_EVENT_STAGE_ACTIVATED), {str(self.funke.pk)})
        kinds = list(WorkflowApproverChange.objects.filter(instance=request)
                     .order_by("changed_at").values_list("kind", flat=True))
        self.assertEqual(kinds, [ApproverChangeKind.DELEGATION_APPLIED,
                                 ApproverChangeKind.DELEGATION_REVOKED])

    def test_a_delegate_who_has_decided_stays_after_the_revoke(self):
        self.unanimous(self.hod)
        request = self.raise_request()
        self.set(request, self.hod, self.funke, self.okafor)
        created = TenantAPIClient(self.funke).post(
            f"{BASE}/delegations/", self.body(), format="json")
        actions.record_action(request.pk, self.tunde, Vote.APPROVED)
        TenantAPIClient(self.funke).post(f"{BASE}/delegations/{created.data['id']}/revoke/")
        self.assertIn((self.tunde.pk, self.funke.pk), self.seats(request))

    def test_an_administrator_sees_and_revokes_every_delegation(self):
        created = TenantAPIClient(self.funke).post(
            f"{BASE}/delegations/", self.body(), format="json")
        listed = TenantAPIClient(self.admin).get(f"{BASE}/delegations/")
        rows = listed.data["data"] if "data" in listed.data else listed.data
        self.assertIn(created.data["id"], [row["id"] for row in rows])
        revoked = TenantAPIClient(self.admin).post(
            f"{BASE}/delegations/{created.data['id']}/revoke/")
        self.assertEqual(revoked.status_code, 200, revoked.data)
        own = TenantAPIClient(self.okafor).get(f"{BASE}/delegations/")
        own_rows = own.data["data"] if "data" in own.data else own.data
        self.assertEqual(own_rows, [])
