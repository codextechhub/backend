"""No account security fact or staff bank detail reaches a caller who may read none.

Every declared surface of ``platform.team`` and ``platform.staff_profile`` is
rendered on a real account or staff profile whose registered fields are filled
in, as a caller whose role has every field of that resource switched off and
who is not the person the record is about. The whole payload is walked.

``platform.team`` is rendered in a tenant with two branches and in one with a
single branch; ``platform.staff_profile`` exists only on the platform tenant.
"""
from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import (
    make_branch,
    make_school,
    make_school_admin,
    make_vision_user,
    platform_tenant,
)
from vs_user.models import PlatformStaffProfile, User

_SURFACE = "vs_user.serializers."


class AccountDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({"platform.team", "platform.staff_profile"})
    covers = frozenset({
        _SURFACE + "UserReadSerializer",
        _SURFACE + "UserListSerializer",
        _SURFACE + "PlatformStaffProfileSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        multi = make_school(slug="deep-user-multi", name="Corona Group").tenant
        ikeja = make_branch(multi, name="Ikeja Branch")
        make_branch(multi, name="Lekki Branch", is_main=False)
        solo = make_school(slug="deep-user-solo", name="Single Site").tenant
        solo_main = make_branch(solo, name="Main Branch")
        cls.tenant = multi
        cls.accounts = []
        for tenant, branch, slug in ((multi, ikeja, "multi"), (solo, solo_main, "solo")):
            inviter = make_school_admin(
                None, email=f"inviter-{slug}@deep-user.test", tenant=tenant,
            )
            account = make_school_admin(
                branch, email=f"member-{slug}@deep-user.test", tenant=tenant,
            )
            account.invited_by = inviter
            account.password_changed_at = timezone.now()
            account.last_login_at = timezone.now()
            account.save(update_fields=["invited_by", "password_changed_at", "last_login_at"])
            cls.accounts.append((tenant, account))

        cls.platform = platform_tenant()
        colleague = make_vision_user(email="colleague@deep-user.test")
        cls.profile = PlatformStaffProfile.objects.create(
            user=colleague, employee_id="CX-DEEP-1", job_title="Analyst",
            bank_name="GTBank", account_name="Colleague Name",
            account_number="0123456789",
        )

    def samples(self):
        samples = []
        for tenant, account in self.accounts:
            account = User.objects.select_related("tenant", "branch", "invited_by").get(
                pk=account.pk,
            )
            samples += [
                Sample(_SURFACE + "UserReadSerializer", account, tenant=tenant),
                Sample(_SURFACE + "UserListSerializer", [account], many=True, tenant=tenant),
            ]
        samples.append(Sample(
            _SURFACE + "PlatformStaffProfileSerializer",
            PlatformStaffProfile.objects.get(pk=self.profile.pk), tenant=self.platform,
        ))
        return samples
