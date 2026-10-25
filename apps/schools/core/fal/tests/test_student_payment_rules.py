"""Whether a school's pupils may pay their own fees, its smallest part payment, and card.

Corona's bursar reads and changes the three rules; a branch-bound bursar at
Lekki may read them but not change them, because they bind every branch; a
member of staff without the fee key may do neither. Greenfield's rules are its
own.
"""
from __future__ import annotations

from django.core.management import call_command
from django.urls import reverse

from core.test_utils import TenantAPIClient

from .base import FALFixture

URL = "fal-student-payment-rules"


class StudentPaymentRulesTests(FALFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_config_catalogue", verbosity=0)
        for key in ("school.fees.view", "school.fees.update"):
            cls.grant(cls.bursar, key)
            cls.grant(cls.lekki_bursar, key, branch=cls.lekki)
            cls.grant(cls.greenfield_bursar, key)
        cls.clerk = cls.user_for(cls.corona, "clerk@corona.test")

    def read(self, user):
        return TenantAPIClient(user).get(reverse(URL))

    def change(self, user, body):
        return TenantAPIClient(user).patch(reverse(URL), body, format="json")

    def test_a_school_that_has_set_nothing_lets_pupils_pay_from_a_thousand_naira_and_takes_no_card(self):
        data = self.read(self.bursar).data["data"]
        self.assertEqual(
            data, {"enabled": True, "min_part_payment": 100_000, "family_card_payments": False},
        )

    def test_the_bursar_turns_pupil_payments_off_and_raises_the_minimum(self):
        response = self.change(self.bursar, {"enabled": False, "min_part_payment": 500_000})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["data"],
            {"enabled": False, "min_part_payment": 500_000, "family_card_payments": False},
        )

    def test_the_bursar_switches_card_payments_for_families_on_and_off(self):
        on = self.change(self.bursar, {"family_card_payments": True})
        self.assertEqual(on.status_code, 200, on.data)
        self.assertTrue(on.data["data"]["family_card_payments"])
        self.assertTrue(self.read(self.bursar).data["data"]["family_card_payments"])
        off = self.change(self.bursar, {"family_card_payments": False})
        self.assertFalse(off.data["data"]["family_card_payments"])

    def test_card_payments_must_be_true_or_false(self):
        response = self.change(self.bursar, {"family_card_payments": "sometimes"})
        self.assertEqual(response.status_code, 400)

    def test_card_payments_are_stored_under_the_school_setting(self):
        from vs_config.models import ConfigurationValue

        self.change(self.bursar, {"family_card_payments": True})
        stored = ConfigurationValue.all_objects.get(
            definition__key="fees.family_card_payments_enabled", tenant=self.corona.tenant,
        )
        self.assertIs(stored.value, True)

    def test_a_branch_bound_bursar_cannot_change_them(self):
        self.assertEqual(self.read(self.lekki_bursar).status_code, 200)
        response = self.change(self.lekki_bursar, {"enabled": False})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.read(self.bursar).data["data"]["enabled"])

    def test_a_branch_bound_bursar_cannot_switch_card_payments(self):
        response = self.change(self.lekki_bursar, {"family_card_payments": True})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.read(self.bursar).data["data"]["family_card_payments"])

    def test_without_the_fee_key_nothing_is_read_or_changed(self):
        self.assertEqual(self.read(self.clerk).status_code, 403)
        self.assertEqual(self.change(self.clerk, {"enabled": False}).status_code, 403)
        self.assertEqual(self.change(self.clerk, {"family_card_payments": True}).status_code, 403)

    def test_a_minimum_that_is_not_whole_naira_is_refused(self):
        response = self.change(self.bursar, {"min_part_payment": 100_050})
        self.assertEqual(response.status_code, 400)

    def test_the_minimum_is_stored_in_naira_as_a_person_typed_it(self):
        from vs_config.models import ConfigurationValue

        self.change(self.bursar, {"min_part_payment": 250_000})
        stored = ConfigurationValue.all_objects.get(definition__key="fees.min_part_payment")
        self.assertEqual(stored.value, 2_500)

    def test_a_negative_minimum_is_refused(self):
        response = self.change(self.bursar, {"min_part_payment": -1})
        self.assertEqual(response.status_code, 400)

    def test_one_schools_rules_never_reach_another(self):
        self.change(self.bursar, {"enabled": False, "family_card_payments": True})
        data = self.read(self.greenfield_bursar).data["data"]
        self.assertTrue(data["enabled"])
        self.assertFalse(data["family_card_payments"])
