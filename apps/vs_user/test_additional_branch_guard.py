"""Equal account postings obey the same tenant boundary as the anchor branch."""

from django.db import IntegrityError, transaction
from django.test import TestCase

from vs_rbac.tests.helpers import (
    make_branch,
    make_school,
    make_staff_user,
    make_vision_user,
)


class AdditionalBranchGuardTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="posting-guard", name="Posting Guard")
        self.branch = make_branch(self.school)
        self.other = make_school(slug="posting-guard-other", name="Other School")
        self.foreign = make_branch(self.other)

    def test_platform_account_cannot_hold_an_additional_branch(self):
        person = make_vision_user(email="posting-guard-platform@test.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            person.additional_branches.add(self.branch)
        self.assertFalse(person.additional_branches.exists())

    def test_account_cannot_hold_another_tenants_branch(self):
        person = make_staff_user(self.branch, email="posting-guard-staff@test.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            person.additional_branches.add(self.foreign)
        self.assertFalse(person.additional_branches.exists())

    def test_existing_additional_posting_blocks_tenant_change(self):
        person = make_staff_user(None, tenant=self.school.tenant, email="posting-guard-move@test.com")
        person.additional_branches.add(self.branch)
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(person).objects.filter(pk=person.pk).update(tenant=self.other.tenant)

    def test_existing_additional_posting_blocks_platform_transition(self):
        person = make_staff_user(None, tenant=self.school.tenant, email="posting-guard-kind@test.com")
        person.additional_branches.add(self.branch)
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(self.school.tenant).objects.filter(pk=self.school.tenant.pk).update(kind="PLATFORM")
