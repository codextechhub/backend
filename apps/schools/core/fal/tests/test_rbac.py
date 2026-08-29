"""Component 4: a permission is evaluated in the school that owns the books."""

from __future__ import annotations

from schools.core.fal.adapters.django_finance import DjangoFinanceRbacAdapter
from schools.core.fal.exceptions import CrossTenantError, EntityNotProvisioned

from .base import FALFixture

KEY = "finance.feestructure.generate"


class FinanceRbacTests(FALFixture):
    def setUp(self):
        super().setUp()
        self.port = DjangoFinanceRbacAdapter()

    def test_a_key_held_in_one_school_does_not_reach_another(self):
        """The whole reason this port exists.

        The Greenfield bursar holds fee-generation there. Asked the same question
        about Corona's books, the answer has to be no, or one school's staff can
        raise invoices against another school's ledger.
        """
        self.grant(self.greenfield_bursar, KEY)

        allowed_at_home = self.port.can(
            self.greenfield_bursar.pk, KEY,
            entity_ref=self.greenfield_books.entity_ref,
        ).unwrap()
        allowed_next_door = self.port.can(
            self.greenfield_bursar.pk, KEY, entity_ref=self.corona_books.entity_ref,
        ).unwrap()

        self.assertTrue(allowed_at_home)
        self.assertFalse(allowed_next_door)

    def test_a_user_without_the_key_is_denied(self):
        self.assertFalse(
            self.port.can(
                self.bursar.pk, KEY, entity_ref=self.corona_books.entity_ref,
            ).unwrap()
        )

    def test_a_vision_super_admin_holding_no_key_is_denied(self):
        """Deliberate, and different from what the same person sees at a view.

        ``is_vision_super_admin`` short-circuits inside the DRF permission
        classes only. The evaluator has no bypass, so a programmatic check
        answers on the keys actually held. Consumers must not assume the two
        agree.
        """
        from vs_rbac.permissions import is_vision_super_admin
        from vs_rbac.tests.helpers import make_vision_user

        admin = make_vision_user(email="super@codex.test", super_admin=True)
        self.assertTrue(is_vision_super_admin(admin))

        self.assertFalse(
            self.port.can(
                admin.pk, KEY, entity_ref=self.corona_books.entity_ref,
            ).unwrap()
        )

    def test_a_branch_pinned_grant_counts_when_no_branch_is_named(self):
        """The sentinel, not None.

        Corona's Lekki bursar holds fee generation for Lekki only. A dashboard
        asking "may this person generate fees?" names no branch. If the FAL
        forwarded ``branch=None`` the evaluator would read that as the narrower
        question "for the entity as a whole?" and answer no, and a bursar with a
        real grant would be locked out of everything.
        """
        self.grant(self.lekki_bursar, KEY, branch=self.lekki)

        self.assertTrue(
            self.port.can(
                self.lekki_bursar.pk, KEY, entity_ref=self.corona_books.entity_ref,
            ).unwrap()
        )

    def test_a_branch_pinned_grant_does_not_reach_another_branch(self):
        self.grant(self.lekki_bursar, KEY, branch=self.lekki)

        self.assertFalse(
            self.port.can(
                self.lekki_bursar.pk, KEY,
                entity_ref=self.corona_books.entity_ref, branch_ref=self.ikeja.pk,
            ).unwrap()
        )

    def test_a_branch_pinned_grant_holds_at_its_own_branch(self):
        self.grant(self.lekki_bursar, KEY, branch=self.lekki)

        self.assertTrue(
            self.port.can(
                self.lekki_bursar.pk, KEY,
                entity_ref=self.corona_books.entity_ref, branch_ref=self.lekki.pk,
            ).unwrap()
        )

    def test_a_branch_from_another_school_is_refused(self):
        self.grant(self.bursar, KEY)

        with self.assertRaises(CrossTenantError):
            self.port.can(
                self.bursar.pk, KEY, entity_ref=self.corona_books.entity_ref,
                branch_ref=self.greenfield_main.pk,
            )

    def test_an_unknown_user_is_denied_rather_than_erroring(self):
        self.assertFalse(
            self.port.can(
                9_999_999, KEY, entity_ref=self.corona_books.entity_ref,
            ).unwrap()
        )

    def test_an_unresolvable_entity_raises(self):
        with self.assertRaises(EntityNotProvisioned):
            self.port.can(self.bursar.pk, KEY, entity_ref=9_999_999)
