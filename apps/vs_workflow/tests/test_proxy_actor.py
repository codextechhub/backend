"""An approval cast under a proxy says who really cast it.

Ada Obi proxies Chioma Okafor, who is an approver on the stage, and approves.
The vote is Chioma's: she is the eligible approver and her vote counts toward
the stage. The vote also records Ada as ``proxied_by``, the engine's audit row
names Ada as the actor and Chioma as whom she acted for, and the approver
timeline reads "Ada Obi for Chioma Okafor". A vote Chioma casts herself reads
"Chioma Okafor".
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from vs_admin_console.models import ImpersonationSession
from vs_tenants.context import clear_request_context, set_current_audit_identity
from vs_workflow.constants import AuditEventType, WorkflowStageAction as ActionEnum
from vs_workflow.models import WorkflowAuditLog, WorkflowStageAction
from vs_workflow.serializers import (
    WorkflowAuditLogReadSerializer, WorkflowStageActionReadSerializer,
    WorkflowStageInstanceReadSerializer,
)
from vs_workflow.services import actions as svc

from .test_actions import (
    _make_approver, _make_instance, _make_stage, _make_stage_instance,
    _make_template, _platform_tenant,
)

EXISTING_VOTE_KEYS = {
    "id", "action", "actor", "on_behalf_of", "comment", "attempt",
    "acted_at", "reversed_at", "reversed_by", "reversal_reason", "is_reversal_of",
}
EXISTING_AUDIT_KEYS = {
    "id", "event_type", "actor", "stage_instance", "context", "message", "occurred_at",
}


def _person(email, first, last):
    return get_user_model().objects.create_user(
        email=email, tenant=_platform_tenant(), first_name=first, last_name=last,
    )


class _ProxyVoteFixture(TestCase):
    """One active stage with Chioma as its approver, and Ada able to proxy her."""

    def setUp(self):
        self.requester = _person("req-proxy@test.com", "Emeka", "Obi")
        self.chioma = _person("chioma-proxy@test.com", "Chioma", "Okafor")
        self.ada = _person("ada-proxy@test.com", "Ada", "Obi")
        template = _make_template()
        stage = _make_stage(template)
        self.instance = _make_instance(template, self.requester, stage)
        self.si = _make_stage_instance(self.instance, stage)
        _make_approver(self.si, self.chioma)
        self.session = ImpersonationSession.objects.create(
            staff_user=self.ada, tenant=self.chioma.tenant, target_user=self.chioma,
            justification="Covering approvals.",
        )

    def tearDown(self):
        clear_request_context()

    def proxy(self):
        set_current_audit_identity(
            actor_user=self.ada, effective_user=self.chioma,
            impersonation_session=self.session,
        )

    def approve(self, actor):
        with patch("vs_workflow.services.actions.routing_service"):
            svc.record_action(self.instance.id, actor, ActionEnum.APPROVED)
        return WorkflowStageAction.objects.get(stage_instance=self.si, actor=actor)

    def acted_log(self):
        return WorkflowAuditLog.objects.get(
            instance=self.instance, event_type=AuditEventType.APPROVER_ACTED,
        )


class ProxiedVoteTests(_ProxyVoteFixture):

    def test_the_vote_is_chiomas_and_records_ada_as_the_real_voter(self):
        self.proxy()

        vote = self.approve(self.chioma)

        self.assertEqual(vote.actor_id, self.chioma.pk)
        self.assertEqual(vote.proxied_by_id, self.ada.pk)

    def test_the_audit_row_names_ada_acting_for_chioma(self):
        self.proxy()
        self.approve(self.chioma)

        log = self.acted_log()

        self.assertEqual(log.actor_id, self.ada.pk)
        self.assertEqual(log.effective_user_id, self.chioma.pk)

    def test_the_timeline_reads_ada_for_chioma(self):
        self.proxy()
        vote = self.approve(self.chioma)
        clear_request_context()

        row = WorkflowStageActionReadSerializer(vote).data
        stage_row = WorkflowStageInstanceReadSerializer(self.si).data
        log_row = WorkflowAuditLogReadSerializer(self.acted_log()).data

        self.assertEqual(row["acted_label"], "Ada Obi for Chioma Okafor")
        self.assertEqual(row["real_actor_name"], "Ada Obi")
        self.assertEqual(row["proxied_user_name"], "Chioma Okafor")
        self.assertEqual(row["actor"], self.chioma.pk)
        self.assertEqual(row["proxied_by"], self.ada.pk)
        self.assertTrue(EXISTING_VOTE_KEYS <= set(row))
        self.assertEqual(stage_row["actions"][0]["acted_label"], "Ada Obi for Chioma Okafor")
        self.assertEqual(log_row["acted_label"], "Ada Obi for Chioma Okafor")
        self.assertEqual(log_row["effective_user"], self.chioma.pk)
        self.assertTrue(EXISTING_AUDIT_KEYS <= set(log_row))

    def test_a_vote_by_someone_other_than_the_proxied_person_is_not_attributed(self):
        other = _person("other-proxy@test.com", "Tunde", "Bello")
        _make_approver(self.si, other)
        self.proxy()

        vote = self.approve(other)

        self.assertIsNone(vote.proxied_by_id)


class UnproxiedVoteTests(_ProxyVoteFixture):

    def test_a_vote_cast_in_person_shows_only_the_voter(self):
        vote = self.approve(self.chioma)

        self.assertIsNone(vote.proxied_by_id)
        log = self.acted_log()
        self.assertEqual(log.actor_id, self.chioma.pk)
        self.assertIsNone(log.effective_user_id)
        row = WorkflowStageActionReadSerializer(vote).data
        self.assertEqual(row["acted_label"], "Chioma Okafor")
        self.assertIsNone(row["real_actor_name"])
        self.assertIsNone(row["proxied_user_name"])
        self.assertIsNone(row["proxied_by"])
        self.assertTrue(EXISTING_VOTE_KEYS <= set(row))
        log_row = WorkflowAuditLogReadSerializer(log).data
        self.assertEqual(log_row["acted_label"], "Chioma Okafor")
        self.assertIsNone(log_row["effective_user"])


class ProxySeparationOfDutiesTests(TestCase):
    """Separation of duties counts the real person, whichever identity they use.

    Ada Obi raises a document; Chioma Okafor is the approver on its stage; Ada
    may proxy Chioma. Ada approving her own document through Chioma's identity
    is refused as self-approval. Chioma approving it herself is ordinary, and so
    is Ada proxying Chioma on a document Bola Ade raised. There is no rule
    across steps: nothing here stops one person signing two steps of a
    document somebody else raised.

    Within one step, a person has one vote whichever identity casts it. Ada
    approving Bola's document as herself and again as Chioma, in either order,
    is refused as a duplicate, as is Ada voting under two different proxies,
    and a quorum is counted in real people.
    """

    def setUp(self):
        self.ada = _person("ada-sod@test.com", "Ada", "Obi")
        self.chioma = _person("chioma-sod@test.com", "Chioma", "Okafor")
        self.bola = _person("bola-sod@test.com", "Bola", "Ade")
        self.template = _make_template()
        self.stage = _make_stage(self.template)
        self.session = ImpersonationSession.objects.create(
            staff_user=self.ada, tenant=self.chioma.tenant, target_user=self.chioma,
            justification="Covering approvals.",
        )

    def tearDown(self):
        clear_request_context()

    def proxy(self):
        set_current_audit_identity(
            actor_user=self.ada, effective_user=self.chioma,
            impersonation_session=self.session,
        )

    def submitted(self, requester, *, proxied=False, stage=None):
        """An instance ``requester`` raised, with Chioma as its approver."""
        from vs_workflow.services import audit as audit_service

        stage = stage or self.stage
        instance = _make_instance(self.template, requester, stage)
        if proxied:
            self.proxy()
        audit_service.write(instance, AuditEventType.INSTANCE_SUBMITTED, actor=requester)
        clear_request_context()
        si = _make_stage_instance(instance, stage)
        _make_approver(si, self.chioma)
        return instance, si

    def proxy_of(self, target):
        """Ada at the keyboard as ``target``, under a session of its own."""
        session = ImpersonationSession.objects.create(
            staff_user=self.ada, tenant=target.tenant, target_user=target,
            justification="Covering approvals.",
        )
        set_current_audit_identity(
            actor_user=self.ada, effective_user=target, impersonation_session=session,
        )

    def approve(self, instance, actor):
        with patch("vs_workflow.services.actions.routing_service"):
            return svc.record_action(instance.id, actor, ActionEnum.APPROVED)

    def test_ada_proxying_chioma_cannot_approve_her_own_request(self):
        from vs_workflow.exceptions import RequesterCannotApproveError

        instance, si = self.submitted(self.ada)
        self.proxy()

        with self.assertRaises(RequesterCannotApproveError) as ctx:
            self.approve(instance, self.chioma)

        self.assertEqual(ctx.exception.message,
                         "You cannot decide this request, because you raised it.")
        self.assertFalse(WorkflowStageAction.objects.filter(stage_instance=si).exists())

    def test_chioma_herself_approves_adas_request(self):
        instance, si = self.submitted(self.ada)

        self.approve(instance, self.chioma)

        vote = WorkflowStageAction.objects.get(stage_instance=si)
        self.assertEqual(vote.actor_id, self.chioma.pk)
        self.assertIsNone(vote.proxied_by_id)

    def test_ada_proxying_chioma_approves_a_request_bola_raised(self):
        instance, si = self.submitted(self.bola)
        self.proxy()

        self.approve(instance, self.chioma)

        vote = WorkflowStageAction.objects.get(stage_instance=si)
        self.assertEqual(vote.actor_id, self.chioma.pk)
        self.assertEqual(vote.proxied_by_id, self.ada.pk)

    def test_the_real_submitter_behind_a_proxy_is_a_requester(self):
        from vs_workflow.services.approvers import requester_ids

        instance, _si = self.submitted(self.chioma, proxied=True)

        self.assertEqual(requester_ids(instance), {self.chioma.pk, self.ada.pk})

    def test_ada_who_submitted_as_chioma_cannot_approve_as_herself(self):
        from vs_workflow.exceptions import RequesterCannotApproveError

        instance, si = self.submitted(self.chioma, proxied=True)
        _make_approver(si, self.ada)

        with self.assertRaises(RequesterCannotApproveError):
            self.approve(instance, self.ada)

    # -- one vote per real person on a step --------------------------------- #

    DUPLICATE = "You have already voted on this stage."

    def open_step(self, code):
        """A QUORUM-of-two step, still open after one approval."""
        stage = _make_stage(self.template, code=code, advance_rule="QUORUM", order=2)
        stage.quorum_count = 2
        stage.save(update_fields=["quorum_count"])
        return self.submitted(self.bola, stage=stage)

    def assert_duplicate(self, instance, actor):
        from vs_workflow.exceptions import DuplicateApproverActionError

        with self.assertRaises(DuplicateApproverActionError) as ctx:
            self.approve(instance, actor)
        self.assertEqual(ctx.exception.message, self.DUPLICATE)

    def test_ada_cannot_approve_as_herself_and_then_as_chioma(self):
        instance, si = self.open_step("dup1")
        _make_approver(si, self.ada)
        self.approve(instance, self.ada)

        self.proxy()
        self.assert_duplicate(instance, self.chioma)

        self.assertEqual(
            list(WorkflowStageAction.objects.filter(stage_instance=si)
                 .values_list("actor_id", flat=True)),
            [self.ada.pk],
        )

    def test_ada_cannot_approve_as_chioma_and_then_as_herself(self):
        instance, si = self.open_step("dup2")
        _make_approver(si, self.ada)
        self.proxy()
        self.approve(instance, self.chioma)
        clear_request_context()

        self.assert_duplicate(instance, self.ada)

        self.assertEqual(WorkflowStageAction.objects.filter(stage_instance=si).count(), 1)

    def test_ada_cannot_vote_under_two_different_proxies(self):
        tunde = _person("tunde-sod@test.com", "Tunde", "Bello")
        instance, si = self.open_step("dup3")
        _make_approver(si, tunde)
        self.proxy()
        self.approve(instance, self.chioma)
        clear_request_context()

        self.proxy_of(tunde)
        self.assert_duplicate(instance, tunde)

        self.assertEqual(WorkflowStageAction.objects.filter(stage_instance=si).count(), 1)

    def test_a_quorum_counts_real_people_not_votes(self):
        from vs_workflow.services.actions import _stage_fully_approved

        tunde = _person("tunde-quorum@test.com", "Tunde", "Bello")
        stage = _make_stage(self.template, code="q2", advance_rule="QUORUM", order=2)
        stage.quorum_count = 2
        stage.save(update_fields=["quorum_count"])
        _instance, si = self.submitted(self.bola, stage=stage)
        WorkflowStageAction.objects.create(
            stage_instance=si, actor=self.ada, action=ActionEnum.APPROVED, attempt=1,
        )
        WorkflowStageAction.objects.create(
            stage_instance=si, actor=self.chioma, proxied_by=self.ada,
            action=ActionEnum.APPROVED, attempt=1,
        )

        self.assertFalse(_stage_fully_approved(si))

        WorkflowStageAction.objects.create(
            stage_instance=si, actor=tunde, action=ActionEnum.APPROVED, attempt=1,
        )
        self.assertTrue(_stage_fully_approved(si))

    def test_a_proxied_vote_and_the_proxied_persons_colleague_make_a_quorum(self):
        from vs_workflow.constants import WorkflowStageStatus

        tunde = _person("tunde-quorum2@test.com", "Tunde", "Bello")
        stage = _make_stage(self.template, code="q2b", advance_rule="QUORUM", order=2)
        stage.quorum_count = 2
        stage.save(update_fields=["quorum_count"])
        instance, si = self.submitted(self.bola, stage=stage)
        _make_approver(si, tunde)

        self.proxy()
        self.approve(instance, self.chioma)
        clear_request_context()
        si.refresh_from_db()
        self.assertEqual(si.status, WorkflowStageStatus.ACTIVE)

        self.approve(instance, tunde)
        si.refresh_from_db()
        self.assertEqual(si.status, WorkflowStageStatus.APPROVED)

    def test_the_queue_drops_a_step_ada_already_voted_on_by_proxy(self):
        from vs_workflow.services.my_queue import pending_approval_snapshots

        instance, si = self.submitted(self.bola)
        _make_approver(si, self.ada)
        self.assertEqual(len(pending_approval_snapshots(self.ada)), 1)

        self.proxy()
        self.approve(instance, self.chioma)
        clear_request_context()

        self.assertEqual(pending_approval_snapshots(self.ada), [])
