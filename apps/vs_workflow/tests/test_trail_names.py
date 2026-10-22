"""An approval trail names its people for everyone who may read it, and says nothing else about them.

Emeka Obi raises a request and Chioma Okafor approves it. Somebody who may
open the document but not the staff list, Tolu the teacher who raised a
requisition, reads the trail through the same payload, so the payload carries
each person's name beside their id. It carries the name only: an account with
no name on it reads "Unknown user", never its email address.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.person_exit import person_name_in, prime_exit_states
from vs_workflow.serializers import (
    WorkflowAuditLogReadSerializer, WorkflowInstanceListSerializer,
    WorkflowStageInstanceReadSerializer,
)

from .test_actions import _make_approver
from .test_proxy_actor import _ProxyVoteFixture, _person


class TrailNamesTests(_ProxyVoteFixture):

    def test_every_person_on_the_trail_comes_with_their_name(self):
        self.approve(self.chioma)

        stage = WorkflowStageInstanceReadSerializer(self.si).data
        row = WorkflowInstanceListSerializer(self.instance).data
        log = WorkflowAuditLogReadSerializer(self.acted_log()).data

        self.assertEqual(row["requested_by_name"], "Emeka Obi")
        self.assertEqual(stage["eligible_approvers"][0]["user_name"], "Chioma Okafor")
        self.assertIsNone(stage["eligible_approvers"][0]["on_behalf_of_name"])
        self.assertEqual(stage["actions"][0]["actor_name"], "Chioma Okafor")
        self.assertIsNone(stage["actions"][0]["reversed_by_name"])
        self.assertEqual(log["actor_name"], "Chioma Okafor")

    def test_a_proxied_vote_names_both_people(self):
        self.proxy()
        self.approve(self.chioma)

        action = WorkflowStageInstanceReadSerializer(self.si).data["actions"][0]

        self.assertEqual(action["actor_name"], "Chioma Okafor")
        self.assertEqual(action["acted_label"], "Ada Obi for Chioma Okafor")

    def test_a_person_with_no_name_is_not_shown_by_their_email(self):
        nameless = _person("no-name@test.com", "Placeholder", "Name")
        # An account imported before names were required: blank them below the model's validation.
        get_user_model().objects.filter(pk=nameless.pk).update(first_name="", last_name="")
        nameless.refresh_from_db()
        _make_approver(self.si, nameless)
        self.approve(nameless)

        stage = WorkflowStageInstanceReadSerializer(self.si).data
        log = WorkflowAuditLogReadSerializer(self.acted_log()).data

        mine = [a for a in stage["actions"] if a["actor"] == nameless.pk][0]
        self.assertEqual(mine["actor_name"], "Unknown user")
        self.assertEqual(mine["acted_label"], "Unknown user")
        self.assertEqual(log["acted_label"], "Unknown user")
        self.assertNotIn("no-name@test.com", json.dumps([stage, log], default=str))

    def test_no_email_travels_with_the_trail(self):
        self.approve(self.chioma)

        payload = json.dumps([
            WorkflowInstanceListSerializer(self.instance).data,
            WorkflowStageInstanceReadSerializer(self.si).data,
            WorkflowAuditLogReadSerializer(self.acted_log()).data,
        ], default=str)

        for person in (self.requester, self.chioma):
            self.assertNotIn(person.email, payload)


class PrimedNamesTests(TestCase):

    def test_names_come_from_the_lookup_a_response_already_makes(self):
        from .test_actions import _platform_tenant

        people = [
            get_user_model().objects.create_user(
                email=f"primed-{n}@test.com", tenant=_platform_tenant(),
                first_name="Ngozi", last_name=f"Ibekwe {n}",
            )
            for n in range(3)
        ]
        context = {}
        prime_exit_states(context, (p.pk for p in people))

        with self.assertNumQueries(0):
            names = [person_name_in(context, p.pk) for p in people]

        self.assertEqual(names, ["Ngozi Ibekwe 0", "Ngozi Ibekwe 1", "Ngozi Ibekwe 2"])

    def test_no_person_has_no_name_and_an_unknown_id_is_not_shown(self):
        self.assertIsNone(person_name_in({}, None))
        self.assertEqual(person_name_in({}, 987654321), "Unknown user")
