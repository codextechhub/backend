"""Where a school's own app lives, as the Console and the pay links both ask it."""

from django.test import SimpleTestCase, override_settings

from vs_tenants.app_urls import school_app_url


@override_settings(SCHOOL_APP_BASE_URL="https://xvs.codexng.com")
class SchoolAppUrlTests(SimpleTestCase):
    def test_inserts_the_slug_as_a_subdomain(self):
        self.assertEqual(
            school_app_url("bright-star"),
            "https://bright-star.xvs.codexng.com",
        )

    def test_a_new_school_needs_no_configuration(self):
        # Nothing is stored per school, so a tenant registered a minute ago
        # already has an address.
        self.assertEqual(
            school_app_url("registered-today"),
            "https://registered-today.xvs.codexng.com",
        )

    def test_normalises_what_it_is_given(self):
        self.assertEqual(
            school_app_url("  Holy-Cross  "),
            "https://holy-cross.xvs.codexng.com",
        )

    @override_settings(SCHOOL_APP_BASE_URL="http://localhost:5174")
    def test_keeps_the_port_in_development(self):
        # The shape the onboarding seeder prints, and the one a local Console
        # has to link to for the link to be worth showing at all.
        self.assertEqual(
            school_app_url("corona"),
            "http://corona.localhost:5174",
        )

    @override_settings(SCHOOL_APP_BASE_URL="https://xvs.codexng.com/")
    def test_a_trailing_slash_does_not_reach_the_output(self):
        self.assertEqual(
            school_app_url("corona"),
            "https://corona.xvs.codexng.com",
        )

    def test_no_slug_is_no_address(self):
        # The platform tenant has no school app, and a caller must be able to
        # tell that apart from a working link.
        self.assertEqual(school_app_url(""), "")
        self.assertEqual(school_app_url("   "), "")
        self.assertEqual(school_app_url(None), "")

    @override_settings(SCHOOL_APP_BASE_URL="")
    def test_no_configured_host_is_no_address(self):
        self.assertEqual(school_app_url("corona"), "")

    @override_settings(SCHOOL_APP_BASE_URL="xvs.codexng.com")
    def test_a_host_with_no_scheme_is_refused_rather_than_mangled(self):
        # urlsplit reads a bare host as a path, so prefixing it would produce
        # "corona." and nothing else. A blank is honest; that is not.
        self.assertEqual(school_app_url("corona"), "")
