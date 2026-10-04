"""The employment-provider registry stays explicit and domain-neutral."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from core.person_exit import exited_states, register_exit_lookup


class PersonExitRegistryTests(SimpleTestCase):
    def test_registered_provider_returns_explicit_false_for_every_other_id(self):
        seen = []

        def lookup(tenant, user_ids):
            seen.append((tenant, user_ids))
            return {2}

        register_exit_lookup("PERSON_EXIT_TEST", lookup)
        tenant = SimpleNamespace(kind="PERSON_EXIT_TEST")

        states = exited_states(tenant, (1, 2, None, 2))

        self.assertEqual(states, {1: False, 2: True})
        self.assertEqual(seen, [(tenant, {1, 2})])
