"""Who may change a school's own settings: the key, and the reach behind it.

Security, payroll, time zone, staff profile visibility, the profile and the
logo all bind every branch of the school. Holding the key that writes them is
not enough: the caller's reach has to be the whole school. A branch's own
security layer is that branch's, and a caller covering the branch may write it.

Bright Star runs Ikeja and Lekki. Adaeze administers the whole school. Ngozi
administers Ikeja: her role carries every key Adaeze's does, pinned to Ikeja,
which is how a school gives a branch administrator the settings key to look
after her own branch's switches.
"""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_config.clock import TIME_ZONE_KEY
from vs_config.models import ConfigurationAuditEvent, ConfigurationDefinition
from vs_config.runtime_settings import resolve_security_settings
from vs_config.services.resolution import resolve_value
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

from .models import School
from .tests_profile_endpoint import ONE_PIXEL_PNG

SECURITY_URL = "/v1/i/me/settings/security/"
PAYROLL_URL = "/v1/i/me/settings/payroll-scope/"
DISPLAY_URL = "/v1/i/me/settings/display/"
STAFF_PROFILES_URL = "/v1/i/me/settings/staff-profiles/"
PROFILE_URL = "/v1/i/me/profile/"
LOGO_URL = "/v1/i/me/profile/logo/"

REFUSED = "SHARED_RECORD_READ_ONLY"

KEYS = (
    "school.settings.view",
    "school.settings.update",
    "school.field_access.update",
    "school.profile.view",
    "school.profile.update",
)


class _ReachFixture(TestCase):
    """A two-branch live school, its whole-school administrator and Ikeja's."""

    @classmethod
    def setUpTestData(cls):
        cls.bright = make_school(slug="bright-star-reach", name="Bright Star")
        cls.tenant = cls.bright.tenant
        cls.ikeja = make_branch(cls.bright, name="Ikeja Branch")
        cls.lekki = make_branch(cls.bright, name="Lekki Branch", is_main=False)

        role = make_role(cls.bright, name="Administrator", key="administrator")
        for key in KEYS:
            make_role_permission(role, make_permission(key))

        cls.adaeze = make_school_admin(cls.ikeja, email="adaeze@bright-reach.example.com")
        make_assignment(cls.bright, cls.adaeze, role, branch=None)
        cls.ngozi = make_school_admin(cls.ikeja, email="ngozi@bright-reach.example.com")
        make_assignment(cls.bright, cls.ngozi, role, branch=cls.ikeja)

    def send(self, user, method, url, body=None, *, fmt="json", **query):
        suffix = "".join(f"&{key}={value}" for key, value in query.items())
        client = TenantAPIClient(user=user)
        return getattr(client, method)(
            f"{url}?tenant={self.tenant.slug}{suffix}", body, format=fmt,
        )

    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], REFUSED)
        self.assertEqual(response.data["message"], message)


class SecuritySettingsReachTests(_ReachFixture):

    def overrides(self, branch=None):
        return resolve_security_settings(tenant=self.tenant, branch=branch)["overrides"]

    def test_the_schools_layer_is_refused_to_a_branch_bound_caller_and_nothing_moves(self):
        response = self.send(self.ngozi, "patch", SECURITY_URL, {"failed_login_threshold": 4})
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the school's security "
            "settings. Choose one of your branches to set its own.",
        )
        self.assertFalse(self.overrides()["failed_login_threshold"])

    def test_a_branch_bound_caller_writes_their_own_branchs_layer(self):
        response = self.send(
            self.ngozi, "patch", SECURITY_URL, {"failed_login_threshold": 4},
            branch=self.ikeja.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["source_scopes"]["failed_login_threshold"], "branch")
        self.assertFalse(self.overrides()["failed_login_threshold"])

    def test_another_branchs_layer_is_a_404_to_a_branch_bound_caller(self):
        response = self.send(
            self.ngozi, "patch", SECURITY_URL, {"failed_login_threshold": 4},
            branch=self.lekki.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(self.overrides(self.lekki)["failed_login_threshold"])

    def test_a_school_wide_caller_writes_the_schools_layer(self):
        response = self.send(self.adaeze, "patch", SECURITY_URL, {"failed_login_threshold": 4})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.overrides()["failed_login_threshold"])


class PayrollScopeReachTests(_ReachFixture):

    def stored(self):
        definition = ConfigurationDefinition.objects.get(key="payroll.scope")
        return resolve_value(definition, tenant=self.tenant)[1]

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        response = self.send(self.ngozi, "patch", PAYROLL_URL, {"scope": "CENTRAL"})
        self.assert_refused(
            response,
            "Only a school-wide administrator can change how the school runs payroll.",
        )
        self.assertIsNone(self.stored())

    def test_a_school_wide_caller_changes_it(self):
        response = self.send(self.adaeze, "patch", PAYROLL_URL, {"scope": "CENTRAL"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(self.stored())


class DisplaySettingsReachTests(_ReachFixture):

    def stored(self, branch=None):
        definition = ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY)
        row = resolve_value(definition, tenant=self.tenant, branch=branch)[1]
        expected = f"branch:{branch.pk}" if branch is not None else f"tenant:{self.tenant.pk}"
        return row if row is not None and row.scope_key == expected else None

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        for body in ({"timezone": "Africa/Nairobi"}, {"date_format": "DD_MM_YYYY"},
                     {"clock": "H24"}):
            with self.subTest(body=body):
                response = self.send(self.ngozi, "patch", DISPLAY_URL, body)
                self.assert_refused(
                    response,
                    "Only a school-wide administrator can change the school's "
                    "display settings. Choose one of your branches to set its "
                    "own time zone.",
                )
        self.assertIsNone(self.stored())
        self.assertFalse(ConfigurationAuditEvent.all_objects.filter(
            tenant=self.tenant, action="config.value.updated",
        ).exists())

    def test_a_branch_bound_caller_sets_and_removes_their_own_branchs_zone(self):
        response = self.send(
            self.ngozi, "patch", DISPLAY_URL, {"timezone": "Africa/Nairobi"},
            branch=self.ikeja.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.ikeja).value, "Africa/Nairobi")
        self.assertIsNone(self.stored())
        removed = self.send(self.ngozi, "delete", DISPLAY_URL, branch=self.ikeja.pk)
        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertIsNone(self.stored(self.ikeja))

    def test_another_branchs_zone_is_a_404_to_a_branch_bound_caller(self):
        for method in ("get", "patch", "delete"):
            with self.subTest(method=method):
                body = {"timezone": "Africa/Nairobi"} if method == "patch" else None
                response = self.send(
                    self.ngozi, method, DISPLAY_URL, body, branch=self.lekki.pk,
                )
                self.assertEqual(response.status_code, 404, response.data)
        self.assertIsNone(self.stored(self.lekki))

    def test_a_school_wide_caller_changes_it(self):
        response = self.send(self.adaeze, "patch", DISPLAY_URL, {"timezone": "Africa/Nairobi"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(self.stored())


class StaffProfileVisibilityReachTests(_ReachFixture):

    BODY = {"policy": {
        "SELF": ["contact", "employment", "personal", "records", "leave", "teaching",
                 "history", "roles"],
        "LINE": ["contact", "employment", "leave", "teaching"],
        "COLLEAGUE": ["contact", "employment"],
    }}

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        response = self.send(self.ngozi, "put", STAFF_PROFILES_URL, self.BODY)
        self.assert_refused(
            response, "Only a school-wide administrator can change who reads staff profiles.",
        )
        self.assertFalse(
            ConfigurationAuditEvent.all_objects.filter(tenant=self.tenant).exists()
        )

    def test_a_school_wide_caller_changes_it(self):
        response = self.send(self.adaeze, "put", STAFF_PROFILES_URL, self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["source"], "school")


class ProfileReachTests(_ReachFixture):

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        response = self.send(self.ngozi, "patch", PROFILE_URL, {"motto": "Ikeja first"})
        self.assert_refused(
            response, "Only a school-wide administrator can change the school's profile.",
        )
        self.assertNotEqual(School.objects.get(pk=self.bright.pk).motto, "Ikeja first")

    def test_a_school_wide_caller_changes_it(self):
        response = self.send(self.adaeze, "patch", PROFILE_URL, {"motto": "Light"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(School.objects.get(pk=self.bright.pk).motto, "Light")


class LogoReachTests(_ReachFixture):

    def upload(self, user):
        return self.send(
            user, "post", LOGO_URL,
            {"logo": SimpleUploadedFile("logo.png", ONE_PIXEL_PNG, content_type="image/png")},
            fmt="multipart",
        )

    def logo(self):
        school = School.objects.select_related("branding").get(pk=self.bright.pk)
        branding = getattr(school, "branding", None)
        return branding.logo.name if branding and branding.logo else ""

    def test_a_branch_bound_caller_cannot_replace_the_logo(self):
        response = self.upload(self.ngozi)
        self.assert_refused(
            response, "Only a school-wide administrator can change the school's logo.",
        )
        self.assertEqual(self.logo(), "")

    def test_a_branch_bound_caller_cannot_remove_the_logo(self):
        self.assertEqual(self.upload(self.adaeze).status_code, 200)
        kept = self.logo()
        self.addCleanup(self.send, self.adaeze, "delete", LOGO_URL)

        response = self.send(self.ngozi, "delete", LOGO_URL)
        self.assert_refused(
            response, "Only a school-wide administrator can change the school's logo.",
        )
        self.assertEqual(self.logo(), kept)
