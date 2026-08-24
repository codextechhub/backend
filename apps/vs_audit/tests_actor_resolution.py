"""An audit event is written even when the caller names its actor loosely.

Written after branch creation left no trail at all. The emitter was handed
``str(user.id)`` where the model wants a User, raised, and swallowed its own
exception - so the failure was invisible and the record simply absent.

Every test here asserts a row EXISTS. A test that only checked "no exception"
would have passed on the broken code, because not raising was exactly the
problem.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.test import TestCase

from vs_audit.models import AuditEvent
from vs_audit.services import emit_audit_event

User = get_user_model()


class ActorResolutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from vs_tenants.models import Tenant

        cls.tenant = Tenant.objects.create(
            slug="bright-star", name="Bright Star", kind=Tenant.Kind.SCHOOL,
        )
        cls.actor = User.objects.create_user(
            email="amaka@bright-star.example.com", password="pw",
            first_name="Amaka", last_name="Obi", status="ACTIVE",
            tenant=cls.tenant,
        )

    def _emit(self, actor):
        emit_audit_event(
            module_key="BRANCH",
            action_type="CREATE",
            entity_type="Branch",
            entity_id="29",
            actor_user=actor,
        )

    def test_a_user_object_records_the_actor(self):
        self._emit(self.actor)

        event = AuditEvent.objects.filter(entity_type="Branch").first()
        self.assertIsNotNone(event, "no audit event was written at all")
        self.assertEqual(event.actor_user, self.actor)

    def test_an_id_string_records_the_actor_too(self):
        """The exact shape that lost the branch-creation trail.

        ``import_executor`` put ``str(queued_by.id)`` into a context key called
        ``actor_id`` and it arrived here as the actor.
        """
        self._emit(str(self.actor.pk))

        event = AuditEvent.objects.filter(entity_type="Branch").first()
        self.assertIsNotNone(event, "an id string still lost the event")
        self.assertEqual(event.actor_user, self.actor)

    def test_an_integer_id_records_the_actor(self):
        self._emit(self.actor.pk)

        event = AuditEvent.objects.filter(entity_type="Branch").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.actor_user, self.actor)

    def test_an_unknown_id_still_writes_the_event(self):
        """An event with no actor beats no event.

        Losing WHO did something is bad. Losing THAT it happened is worse, and
        the old behaviour lost both.
        """
        self._emit("99999999")

        event = AuditEvent.objects.filter(entity_type="Branch").first()
        self.assertIsNotNone(event, "an unresolvable actor swallowed the event")
        self.assertIsNone(event.actor_user)

    def test_unusable_rubbish_is_logged_and_the_event_survives(self):
        with self.assertLogs("vs_audit", level=logging.WARNING) as logged:
            self._emit(object())

        self.assertTrue(
            any("unusable actor" in line for line in logged.output),
            "an unusable actor should say so rather than vanish",
        )
        self.assertIsNotNone(AuditEvent.objects.filter(entity_type="Branch").first())

    def test_no_actor_is_still_a_recorded_event(self):
        self._emit(None)

        event = AuditEvent.objects.filter(entity_type="Branch").first()
        self.assertIsNotNone(event)
        self.assertIsNone(event.actor_user)
