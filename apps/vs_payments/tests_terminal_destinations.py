"""A terminal notifies the numbers its tenant gives it, starting with the branch's own phone.

Lekki's terminal opens with Lekki's phone as its one number, saved under the
branch's name. The bursar adds the head teacher's number and later takes it off
again; she can never take off the last number. A branch with no phone gets no
terminal until someone gives it a number. The numbers are personal data: they
are switched fields, they are never written into the audit trail, and a clerk
at Ikeja has no way to touch Lekki's.
"""
from __future__ import annotations

import itertools
import json

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import install_declared_fields, set_field_access
from vs_rbac.models import TenantRoleTemplate
from vs_tenants.models import Branch

from . import terminals
from .constants import PaymentAuditAction, TerminalStatus
from .exceptions import LastDestinationError, PaymentStateError
from .models import PaymentEvent, VirtualTerminal
from .tests_terminals import _TerminalFixture

_people = itertools.count(1)

LEKKI_NUMBER = "2348010000002"
HEAD_NUMBER = "2348055500001"


class NormaliseWhatsappTests(SimpleTestCase):
    def test_numbers_come_out_in_international_form_without_a_plus(self):
        for raw, expected in (
            ("08012345678", "2348012345678"),
            ("0801 234 5678", "2348012345678"),
            ("+234 801 234 5678", "2348012345678"),
            ("2348012345678", "2348012345678"),
            ("(0801) 234-5678", "2348012345678"),
            ("8012345678", "2348012345678"),
            ("002348012345678", "2348012345678"),
            ("+44 7911 123456", "447911123456"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(terminals.normalise_whatsapp(raw), expected)

    def test_what_is_not_a_number_is_refused(self):
        for raw in ("", None, "abc", "12345", "0801234567", "080123456789", "+0801234567890",
                    "1" * 16):
            with self.subTest(raw=raw):
                self.assertEqual(terminals.normalise_whatsapp(raw), "")

    def test_numbers_in_a_provider_message_are_masked(self):
        self.assertEqual(
            terminals.scrub_numbers("Invalid target 2348010000002 and +2348055500001 (HTTP 422)"),
            "Invalid target [number] and [number] (HTTP 422)")


class DestinationServiceTests(_TerminalFixture):
    def setUp(self):
        super().setUp()
        self.lekki_terminal = self.open(self.lekki)
        self.bursar = self.user_for(self.tenant, f"bursar{next(_people)}@corona.test")

    def numbers(self, terminal=None):
        row = VirtualTerminal.objects.get(pk=(terminal or self.lekki_terminal).pk)
        return [d["number"] for d in row.destinations]

    def test_a_number_is_added_at_the_provider_and_on_the_terminal(self):
        result = terminals.add_destination(
            self.lekki_terminal, name="Head Teacher", number="0805 550 0001", actor_user=self.bursar)

        self.assertEqual(result, [{"name": "Lekki Branch", "number": LEKKI_NUMBER},
                                  {"name": "Head Teacher", "number": HEAD_NUMBER}])
        self.assertEqual(self.numbers(), [LEKKI_NUMBER, HEAD_NUMBER])
        self.assertEqual(self.fake.terminals[self.lekki_terminal.code]["destinations"][-1],
                         {"target": HEAD_NUMBER, "name": "Head Teacher"})

    def test_a_duplicate_a_bad_number_a_missing_name_and_a_sixth_are_refused(self):
        with self.assertRaises(ValidationError):
            terminals.add_destination(self.lekki_terminal, name="Again", number="08010000002")
        with self.assertRaises(ValidationError):
            terminals.add_destination(self.lekki_terminal, name="Nobody", number="12345")
        with self.assertRaises(ValidationError):
            terminals.add_destination(self.lekki_terminal, name="  ", number=HEAD_NUMBER)
        for n in range(4):
            terminals.add_destination(self.lekki_terminal, name=f"Person {n}", number=f"23480555000{n:02d}")
        with self.assertRaises(PaymentStateError) as caught:
            terminals.add_destination(self.lekki_terminal, name="Too many", number="2348077700000")
        self.assertIn("at most 5", str(caught.exception))
        self.assertEqual(len(self.numbers()), 5)

    def test_a_provider_refusal_puts_the_list_back(self):
        self.fake.terminal_error = "Paystack is down"
        with self.assertRaises(Exception):
            terminals.add_destination(self.lekki_terminal, name="Head", number=HEAD_NUMBER)
        self.assertEqual(self.numbers(), [LEKKI_NUMBER])

    def test_a_number_is_removed_at_the_provider_and_on_the_terminal(self):
        terminals.add_destination(self.lekki_terminal, name="Head", number=HEAD_NUMBER)
        result = terminals.remove_destination(
            self.lekki_terminal, number="0805 550 0001", actor_user=self.bursar)
        self.assertEqual([d["number"] for d in result], [LEKKI_NUMBER])
        self.assertEqual(self.numbers(), [LEKKI_NUMBER])
        self.assertEqual([d["target"] for d in self.fake.terminals[self.lekki_terminal.code]["destinations"]],
                         [LEKKI_NUMBER])

    def test_the_last_number_cannot_be_removed(self):
        with self.assertRaises(LastDestinationError) as caught:
            terminals.remove_destination(self.lekki_terminal, number=LEKKI_NUMBER)
        self.assertEqual(str(caught.exception), "A terminal must keep at least one number.")
        self.assertEqual(self.numbers(), [LEKKI_NUMBER])
        self.assertEqual(len(self.fake.terminals[self.lekki_terminal.code]["destinations"]), 1)

    def test_a_number_the_terminal_does_not_have_is_refused(self):
        with self.assertRaises(ValidationError):
            terminals.remove_destination(self.lekki_terminal, number=HEAD_NUMBER)

    def test_a_provider_refusal_to_remove_puts_the_number_back(self):
        terminals.add_destination(self.lekki_terminal, name="Head", number=HEAD_NUMBER)
        self.fake.terminal_error = "Paystack is down"
        with self.assertRaises(Exception):
            terminals.remove_destination(self.lekki_terminal, number=HEAD_NUMBER)
        self.assertEqual(self.numbers(), [LEKKI_NUMBER, HEAD_NUMBER])

    def test_adding_a_number_to_a_waiting_terminal_opens_it(self):
        Branch.all_objects.filter(pk=self.yaba.pk).update(phone="")
        self.yaba.refresh_from_db()
        waiting = terminals.ensure_terminal(self.yaba)
        self.assertEqual(waiting.status, TerminalStatus.NEEDS_NUMBER)

        with self.captureOnCommitCallbacks(execute=True):
            terminals.add_destination(waiting, name="Yaba Office", number="0803 000 1111")

        waiting.refresh_from_db()
        self.assertEqual((waiting.status, waiting.last_error), (TerminalStatus.ACTIVE, ""))
        self.assertEqual(self.fake.terminals[waiting.code]["destinations"],
                         [{"target": "2348030001111", "name": "Yaba Office"}])

    def test_a_deactivated_terminal_takes_no_numbers(self):
        terminals.deactivate_terminal(VirtualTerminal.objects.get(pk=self.lekki_terminal.pk))
        with self.assertRaises(PaymentStateError):
            terminals.add_destination(self.lekki_terminal, name="Head", number=HEAD_NUMBER)

    def test_a_number_never_reaches_the_audit_trail_or_a_stored_error(self):
        terminals.add_destination(self.lekki_terminal, name="Head", number=HEAD_NUMBER,
                                  actor_user=self.bursar)
        terminals.remove_destination(self.lekki_terminal, number=HEAD_NUMBER, actor_user=self.bursar)
        self.fake.terminal_error = f"Invalid target {LEKKI_NUMBER} for {HEAD_NUMBER}"
        failed = terminals.provision_terminal(terminals.ensure_terminal(self.ikeja).pk)
        self.assertEqual(failed.status, TerminalStatus.FAILED)
        self.assertNotIn(LEKKI_NUMBER, failed.last_error)
        self.assertIn("[number]", failed.last_error)

        actions = {PaymentAuditAction.TERMINAL_DESTINATION_ADDED,
                   PaymentAuditAction.TERMINAL_DESTINATION_REMOVED,
                   PaymentAuditAction.TERMINAL_FAILED, PaymentAuditAction.TERMINAL_CREATED}
        events = PaymentEvent.objects.filter(action__in=actions)
        self.assertEqual(events.count(), 4)  # Opened, number added, number removed, a failed open.
        for event in events:
            text = json.dumps([event.message, event.metadata, event.reference])
            for number in (LEKKI_NUMBER, HEAD_NUMBER, "08010000002", "0805 550 0001"):
                self.assertNotIn(number, text)
        for fragment in ("8010000002", "8055500001"):
            self.assertNotIn(fragment, json.dumps(list(PaymentEvent.objects.values_list("message", "metadata"))))


class DestinationApiTests(_TerminalFixture):
    """The three routes are gated by ``payments.terminal.update`` and bound to the caller's branches."""

    ALL = ("payments.terminal.view", "payments.terminal.update")

    def setUp(self):
        super().setUp()
        self.lekki_terminal = self.open(self.lekki)
        self.ikeja_terminal = self.open(self.ikeja)

    def api(self, *keys, tenant=None, branch=None):
        n = next(_people)
        tenant = tenant or self.tenant
        user = self.grant(self.user_for(tenant, f"clerk{n}@{tenant.slug}.test"), *keys,
                          tenant=tenant, role_key=f"clerk-{n}", branch=branch)
        client = TenantAPIClient(user=user)
        client.role = TenantRoleTemplate.objects.get(tenant=tenant, key=f"clerk-{n}")
        return client

    def url(self, terminal, tail="", entity=None):
        return f"/v1/payments/terminals/{terminal.pk}/destinations/{tail}?entity={(entity or self.books).code}"

    def add(self, client, terminal, **body):
        return client.post(self.url(terminal), body, format="json")

    def remove(self, client, terminal, number):
        return client.post(self.url(terminal, "remove/"), {"number": number}, format="json")

    def test_a_caller_without_the_update_key_is_refused_on_every_route(self):
        viewer = self.api("payments.terminal.view", "payments.report.view")
        self.assertEqual(viewer.get(self.url(self.lekki_terminal)).status_code, 403)
        self.assertEqual(self.add(viewer, self.lekki_terminal, name="x", number=HEAD_NUMBER).status_code, 403)
        self.assertEqual(self.remove(viewer, self.lekki_terminal, LEKKI_NUMBER).status_code, 403)
        self.assertEqual(self.api("payments.report.view").get(self.url(self.lekki_terminal)).status_code, 403)

    def test_the_numbers_are_listed_added_and_removed(self):
        admin = self.api(*self.ALL)

        listed = admin.get(self.url(self.lekki_terminal))
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(listed.data["data"], [{"name": "Lekki Branch", "number": LEKKI_NUMBER}])

        added = self.add(admin, self.lekki_terminal, name="Head Teacher", number="08055500001")
        self.assertEqual(added.status_code, 201, added.data)
        self.assertEqual([d["number"] for d in added.data["data"]], [LEKKI_NUMBER, HEAD_NUMBER])

        removed = self.remove(admin, self.lekki_terminal, "+234 805 550 0001")
        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertEqual([d["number"] for d in removed.data["data"]], [LEKKI_NUMBER])

    def test_the_last_number_is_a_409_with_the_plain_sentence(self):
        admin = self.api(*self.ALL)
        response = self.remove(admin, self.lekki_terminal, LEKKI_NUMBER)
        self.assertEqual(response.status_code, 409)
        self.assertIn("A terminal must keep at least one number", json.dumps(response.data))
        self.assertEqual(self.remove(admin, self.lekki_terminal, HEAD_NUMBER).status_code, 400)

    def test_bad_input_is_a_400(self):
        admin = self.api(*self.ALL)
        self.assertEqual(self.add(admin, self.lekki_terminal, name="x", number="12345").status_code, 400)
        self.assertEqual(self.add(admin, self.lekki_terminal, number=HEAD_NUMBER).status_code, 400)
        self.assertEqual(self.add(admin, self.lekki_terminal, name="x", number="08010000002").status_code, 400)

    def test_another_tenants_terminal_is_a_404_on_every_route(self):
        rival = terminals.provision_terminal(terminals.ensure_terminal(self.rival_branch).pk)
        admin = self.api(*self.ALL)
        url = f"/v1/payments/terminals/{rival.pk}/destinations/?entity={self.books.code}"
        self.assertEqual(admin.get(url).status_code, 404)
        self.assertEqual(admin.post(url, {"name": "x", "number": HEAD_NUMBER}, format="json").status_code, 404)
        remove = f"/v1/payments/terminals/{rival.pk}/destinations/remove/?entity={self.books.code}"
        self.assertEqual(admin.post(remove, {"number": "2348010000005"}, format="json").status_code, 404)
        rival.refresh_from_db()
        self.assertEqual([d["number"] for d in rival.destinations], ["2348010000005"])

    def test_a_branch_bound_clerk_manages_their_own_branch_and_no_other(self):
        clerk = self.api(*self.ALL, branch=self.ikeja)
        self.assertEqual(clerk.get(self.url(self.lekki_terminal)).status_code, 404)
        self.assertEqual(self.add(clerk, self.lekki_terminal, name="x", number=HEAD_NUMBER).status_code, 404)
        self.assertEqual(self.remove(clerk, self.lekki_terminal, LEKKI_NUMBER).status_code, 404)
        self.assertEqual([d["number"] for d in VirtualTerminal.objects.get(
            pk=self.lekki_terminal.pk).destinations], [LEKKI_NUMBER])

        mine = self.add(clerk, self.ikeja_terminal, name="Ikeja Office", number=HEAD_NUMBER)
        self.assertEqual(mine.status_code, 201, mine.data)
        self.assertEqual(self.remove(clerk, self.ikeja_terminal, HEAD_NUMBER).status_code, 200)

    def test_a_one_branch_tenant_manages_its_one_terminal(self):
        solo = self.open(self.solo_main)
        admin = self.api(*self.ALL, tenant=self.solo_tenant)
        response = admin.post(
            f"/v1/payments/terminals/{solo.pk}/destinations/?entity={self.solo_books.code}",
            {"name": "Owner", "number": HEAD_NUMBER}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["data"]), 2)

    def test_a_caller_who_may_read_no_numbers_sees_none_and_cannot_probe_by_removing(self):
        admin = self.api(*self.ALL)
        install_declared_fields("payments.terminal_destination")
        set_field_access(admin.role, "payments.terminal_destination.name",
                         "payments.terminal_destination.number", read=False, write=False)
        listed = admin.get(self.url(self.lekki_terminal))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["data"], [{}])
        self.assertNotIn(LEKKI_NUMBER, json.dumps(listed.data))
        self.assertEqual(self.remove(admin, self.lekki_terminal, LEKKI_NUMBER).status_code, 403)
        self.assertEqual(self.remove(admin, self.lekki_terminal, HEAD_NUMBER).status_code, 403)
        # Adding is creating the entry, so it stays open; the response shows nothing.
        added = self.add(admin, self.lekki_terminal, name="Head", number=HEAD_NUMBER)
        self.assertEqual(added.status_code, 201)
        self.assertNotIn(HEAD_NUMBER, json.dumps(added.data))

    def test_the_terminal_list_never_carries_the_numbers(self):
        admin = self.api(*self.ALL)
        terminals.add_destination(self.lekki_terminal, name="Head", number=HEAD_NUMBER)
        response = admin.get(f"/v1/payments/terminals/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200)
        text = json.dumps(response.data)
        for number in (LEKKI_NUMBER, HEAD_NUMBER):
            self.assertNotIn(number, text)

    def test_retry_of_a_waiting_terminal_opens_it_once_the_branch_has_a_phone(self):
        Branch.all_objects.filter(pk=self.yaba.pk).update(phone="")
        self.yaba.refresh_from_db()
        waiting = terminals.ensure_terminal(self.yaba)
        admin = self.api(*self.ALL)
        url = f"/v1/payments/terminals/{waiting.pk}/retry/?entity={self.books.code}"
        first = admin.post(url, {}, format="json")
        self.assertEqual((first.status_code, first.data["data"]["status"]), (200, "NEEDS_NUMBER"))
        self.assertIn("Add the branch's phone number", first.data["data"]["last_error"])
        Branch.all_objects.filter(pk=self.yaba.pk).update(phone="08033300000")
        second = admin.post(url, {}, format="json")
        self.assertEqual(second.data["data"]["status"], "ACTIVE")
