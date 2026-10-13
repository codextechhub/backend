"""No controlled school-profile field reaches a role with Read switched off.

Both the profile shown after sign-in and the serializer used for corrections
are rendered on real schools. Bright Star has two branches and Sunrise has one,
so the school shape cannot accidentally change whether a field is filtered.
"""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import make_branch, make_school, platform_tenant

from .models import PackagePlan, School, SchoolPackageSetup
from .services.packages import apply_plan_entitlements, plan_overview

_SURFACE = "schools.vs_schools.serializers."


class SchoolProfileDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({"school.profile"})
    covers = frozenset({
        _SURFACE + "SchoolProfileSerializer",
        _SURFACE + "SchoolProfileUpdateSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        cls.multi = make_school(slug="bright-star-deep", name="Bright Star Deep")
        make_branch(cls.multi, name="Ikeja Branch", is_main=True)
        make_branch(cls.multi, name="Lekki Branch", is_main=False)
        cls.multi.address = "14 Lantern Road"
        cls.multi.registration_id = "REG-DEEP-MULTI"
        cls.multi.save(update_fields=["address", "registration_id"])

        cls.single = make_school(slug="sunrise-deep", name="Sunrise Deep")
        make_branch(cls.single, name="Main Branch", is_main=True)
        cls.single.address = "3 Sunrise Close"
        cls.single.registration_id = "REG-DEEP-SINGLE"
        cls.single.save(update_fields=["address", "registration_id"])

    def samples(self):
        return [
            Sample(surface, school, tenant=school.tenant)
            for school in (self.multi, self.single)
            for surface in self.covers
        ]


class BranchDeepPayloadTests(DeepPayloadChecks, TestCase):
    """A branch sends no address, email or phone to a role that may read none of them.

    Rendered on a school with two branches and on one with a single branch, so
    the shape of the school cannot change whether the switch is applied.
    """

    resources = frozenset({"school.branches"})
    covers = frozenset({_SURFACE + "SchoolBranchSerializer"})

    @classmethod
    def setUpTestData(cls):
        cls.multi = make_school(slug="branch-deep-multi", name="Branch Deep Multi")
        cls.multi_branches = [
            make_branch(
                cls.multi, name="Ikeja Branch", is_main=True,
                address="14 Lantern Road, Ikeja", email="ikeja@branch-deep.test",
                phone="08011112222",
            ),
            make_branch(
                cls.multi, name="Lekki Branch", is_main=False,
                address="3 Admiralty Way, Lekki", email="lekki@branch-deep.test",
                phone="08033334444",
            ),
        ]
        cls.single = make_school(slug="branch-deep-single", name="Branch Deep Single")
        cls.single_branches = [
            make_branch(
                cls.single, name="Main Branch", is_main=True,
                address="9 Sunrise Close", email="main@branch-deep.test",
                phone="08055556666",
            ),
        ]

    def samples(self):
        return [
            Sample(surface, branches, many=True, tenant=school.tenant)
            for surface in sorted(self.covers)
            for school, branches in (
                (self.multi, self.multi_branches),
                (self.single, self.single_branches),
            )
        ]


class PlatformSchoolsDeepPayloadTests(DeepPayloadChecks, TestCase):
    """No subscription term, price or billing contact reaches a CodeX role that may read none.

    Every surface that carries them is rendered on a school with two branches
    and a depth uplift in place, and on a school with one branch, as a platform
    caller whose role has every ``platform.schools`` field switched off.
    """

    resources = frozenset({"platform.schools"})
    covers = frozenset({
        _SURFACE + name for name in (
            "PackagePlanSerializer",
            "PackagePlanPriceSerializer",
            "ChangeSchoolPlanSerializer",
            "SchoolPackageSetupWriteSerializer",
            "SchoolPackageSetupReadSerializer",
            "SchoolPlanSummarySerializer",
            "SchoolPlanModuleSerializer",
            "SchoolPlanOverviewSerializer",
            "SchoolListSerializer",
            "SchoolDetailSerializer",
            "SchoolCreateSerializer",
            "SchoolUpdateSerializer",
        )
    })

    #: CodeX's own branch records nested in the school detail. A branch email or
    #: phone is not the school's billing email or phone; it is governed by the
    #: platform.branches keys, and the school's own switches for it are
    #: school.branches.email and school.branches.phone.
    allowlist = {
        (_SURFACE + "SchoolDetailSerializer", path):
            "A branch record under platform.branches.view, not the school's billing contact."
        for path in ("main_branch.email", "branches[].email",
                     "main_branch.phone", "branches[].phone")
    }

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        from vs_config.models import Capability, CapabilityDepthGrant

        call_command("seed_actions", verbosity=0)
        call_command("seed_config_catalogue", verbosity=0)
        call_command("seed_package", verbosity=0)
        cls.tenant = platform_tenant()
        cls.plan = PackagePlan.objects.get(code="basic")
        cls.finance = Capability.objects.get(key="finance")

        cls.schools = []
        for slug, branch_names in (
            ("platform-deep-multi", ("Ikeja Branch", "Lekki Branch")),
            ("platform-deep-single", ("Main Branch",)),
        ):
            school = make_school(
                slug=slug, name=slug.title(), email=f"billing@{slug}.test",
                phone="08031234567", address="1 School Road",
            )
            for index, name in enumerate(branch_names):
                make_branch(
                    school, name=name, is_main=index == 0,
                    address=f"{index + 1} Branch Road", email=f"branch{index}@{slug}.test",
                )
            SchoolPackageSetup.objects.create(
                school=school, package_plan=cls.plan,
                subscription_starts_at=date.today(),
                subscription_expires_at=date.today() + timedelta(days=365),
                agreed_price_per_student=350_000, minimum_billable_students=25,
                notes="Agreed on a call with the proprietor.",
            )
            apply_plan_entitlements(
                school=school, plan=cls.plan,
                expires_at=date.today() + timedelta(days=365), actor=None,
            )
            cls.schools.append(school)
        CapabilityDepthGrant.all_objects.create(
            capability=cls.finance, tenant=cls.schools[0].tenant,
            depth=Capability.Depth.ADVANCED, reason="Signed on the promise of payroll.",
            ends_at=timezone.now() + timedelta(days=90),
        )

    def tenant_for(self, resource):
        return self.tenant

    def samples(self):
        samples = []
        for school in self.schools:
            school = School.objects.select_related(
                "branding", "package_setup", "package_setup__package_plan", "tenant",
            ).get(pk=school.pk)
            overview = plan_overview(school)
            samples += [
                Sample(_SURFACE + "SchoolDetailSerializer", school),
                Sample(_SURFACE + "SchoolListSerializer", [school], many=True),
                Sample(_SURFACE + "SchoolCreateSerializer", school),
                Sample(_SURFACE + "SchoolUpdateSerializer", school),
                Sample(_SURFACE + "SchoolPackageSetupReadSerializer", school.package_setup),
                Sample(_SURFACE + "PackagePlanSerializer", school.package_setup.package_plan),
                Sample(_SURFACE + "PackagePlanPriceSerializer", school.package_setup.package_plan),
                Sample(_SURFACE + "SchoolPlanOverviewSerializer", overview),
                Sample(_SURFACE + "SchoolPlanSummarySerializer", overview["plan"]),
                Sample(_SURFACE + "SchoolPlanModuleSerializer", overview["modules"], many=True),
                Sample(
                    _SURFACE + "ChangeSchoolPlanSerializer",
                    {
                        "package_plan": self.plan.code,
                        "subscription_expires_at": date.today() + timedelta(days=365),
                        "agreed_price_per_student": 350_000,
                        "reason": "",
                    },
                ),
                Sample(
                    _SURFACE + "SchoolPackageSetupWriteSerializer",
                    {
                        "package_plan": self.plan,
                        "subscription_starts_at": date.today(),
                        "subscription_expires_at": date.today() + timedelta(days=365),
                        "agreed_price_per_student": 350_000,
                        "minimum_billable_students": 25,
                    },
                ),
            ]
        return samples
