"""The mark in the email header: the sender's own, or the platform's.

Every standard email signed itself "CV" regardless of who sent it, so a parent
opening a fee reminder from Holy Cross saw the school's name in text beside a
product badge they had never been told about.

Most of what is tested here is the template path rather than the live one,
because that is the path real email takes: the document is composed once and
stored on the row, then rendered per recipient. A mark resolved at compose time
would have frozen one school's logo onto every school's email.
"""
from __future__ import annotations

from django.test import TestCase

from .models import NotificationTemplate
from .services.layout import (
    EMAIL_BRAND_LOGO_PLACEHOLDER,
    EMAIL_BRAND_PLACEHOLDER,
    brand_logo_from_context,
    compose_email_html,
)

LOGO = "https://api.codexng.com/v1/i/public/schools/holy-cross/logo/"


def mark_cell(html: str) -> str:
    """The inside of the 42px header square, and nothing else."""
    start = html.find("42px;height:42px")
    assert start != -1, "header mark cell not found in the document"
    return html[start:].split("</td>")[0]


class EmailBrandMarkTests(TestCase):
    """The live path: subject and body already rendered for one recipient."""

    def compose(self, logo):
        return compose_email_html(subject="S", body="B", brand="Holy Cross College",
                                  brand_logo_url=logo)

    def test_a_sender_with_a_logo_signs_the_email_with_it(self):
        cell = mark_cell(self.compose(LOGO))
        self.assertIn(f'src="{LOGO}"', cell)
        self.assertNotIn("CV", cell)

    def test_a_sender_without_one_keeps_the_platform_mark(self):
        self.assertIn("CV", mark_cell(self.compose("")))

    def test_only_http_becomes_an_image(self):
        """The CTA has this rule; the mark is interpolated into the same document.

        A ``javascript:`` or ``data:`` value in an <img src> is not merely
        useless, it is a destination somebody chose, and neither belongs in a
        message we send on a school's behalf.
        """
        for hostile in ("javascript:alert(1)", "data:image/png;base64,AAAA",
                        "//evil.test/x.png", "file:///etc/passwd"):
            with self.subTest(hostile):
                cell = mark_cell(self.compose(hostile))
                self.assertIn("CV", cell)
                self.assertNotIn("<img", cell)

    def test_a_url_cannot_break_out_of_the_attribute(self):
        cell = mark_cell(self.compose('https://evil.test/x.png" onerror="alert(1)'))
        self.assertIn("&quot;", cell)
        self.assertNotIn('onerror="alert(1)"', cell)

    def test_the_image_is_not_announced_twice(self):
        """alt is empty on purpose.

        The sender's name sits beside the mark in text. An alt string would have
        a screen reader read it twice, and would leave the name stranded inside
        a coloured box in the many clients that block remote images until the
        reader asks for them.
        """
        self.assertIn('alt=""', mark_cell(self.compose(LOGO)))


class StoredTemplateMarkTests(TestCase):
    """The path real email takes: composed once, rendered per recipient."""

    def document(self):
        return compose_email_html(
            subject="{{ subject }}", body="Hello.",
            brand=EMAIL_BRAND_PLACEHOLDER,
            brand_logo_url=EMAIL_BRAND_LOGO_PLACEHOLDER,
            as_template=True,
        )

    def test_the_stored_document_defers_the_decision(self):
        """An {% if %}, not an <img>.

        Substituting an empty logo into ``<img src="{{ brand_logo_url }}">``
        yields ``src=""``, which mail clients draw as a broken image - so the
        document has to carry the choice, not just the value.
        """
        cell = mark_cell(self.document())
        self.assertIn("{% if brand_logo_url %}", cell)
        self.assertIn("{% else %}CV{% endif %}", cell)
        self.assertIn("{{ brand_logo_url }}", cell)

    def render(self, context):
        from django.template import Context, Template

        return Template(self.document()).render(Context(context))

    def test_one_document_signs_each_school_with_its_own(self):
        holy = self.render({"email_brand": "Holy Cross", "brand_logo_url": LOGO})
        other = "https://api.codexng.com/v1/i/public/schools/bright-star/logo/"
        bright = self.render({"email_brand": "Bright Star", "brand_logo_url": other})

        self.assertIn(LOGO, mark_cell(holy))
        self.assertIn(other, mark_cell(bright))
        self.assertNotIn(LOGO, mark_cell(bright))

    def test_a_school_with_no_logo_falls_back_rather_than_breaking(self):
        cell = mark_cell(self.render({"email_brand": "Bright Star"}))
        self.assertIn("CV", cell)
        self.assertNotIn("<img", cell)
        self.assertNotIn('src=""', cell)


class TenantLogoUrlTests(TestCase):
    """What the caller puts in the context, and when it puts nothing.

    Lives here rather than in vs_user because it is the other half of the same
    behaviour: this engine deliberately does not know what a school is, so the
    URL has to be built by a caller that does, and the two halves are only
    correct together.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin

        self.school = make_school(slug="holy-cross", name="Holy Cross College")
        self.branch = make_branch(self.school)
        self.user = make_school_admin(self.branch, email="mark@holy-cross.test")

    def logo_url(self):
        from vs_user.tasks import _tenant_logo_url

        return _tenant_logo_url(self.user)

    def test_nothing_when_the_school_has_uploaded_no_logo(self):
        self.assertEqual(self.logo_url(), "")

    def test_the_public_route_when_it_has(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from schools.vs_schools.models import SchoolBranding

        branding = SchoolBranding(school=self.school)
        branding.logo = SimpleUploadedFile(
            "crest.png", b"\x89PNG\r\n\x1a\n-bytes", content_type="image/png",
        )
        branding.save()

        url = self.logo_url()
        # The public route, not /media/: a mail client has no session, and the
        # signed media route refuses an unauthenticated read.
        self.assertTrue(url.endswith("/v1/i/public/schools/holy-cross/logo/"), url)
        self.assertNotIn("/media/", url)
        self.assertTrue(url.startswith("http"), "an email needs an absolute URL")

    def test_nothing_when_no_api_base_is_configured(self):
        """Rather than a relative path a mail client cannot resolve."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from schools.vs_schools.models import SchoolBranding

        branding = SchoolBranding(school=self.school)
        branding.logo = SimpleUploadedFile(
            "crest.png", b"\x89PNG\r\n\x1a\n-bytes", content_type="image/png",
        )
        branding.save()

        with override_settings(API_PUBLIC_BASE_URL=""):
            self.assertEqual(self.logo_url(), "")


class ContextKeyFamilyTests(TestCase):
    """Which context keys brand the mark, and which values are refused.

    The reader is the only guard on the delivering path. ``_brand_mark_html``
    checks the scheme where the document is COMPOSED, but a standard template
    stores ``{{ brand_logo_url }}`` inside the ``src`` and substitutes it at
    send time, so that check never runs for the thirty-one documents anybody
    actually receives. These pin the check that does.
    """

    def test_the_logo_travels_under_the_same_names_as_the_sender(self):
        """A caller that names the sender brands it without a second vocabulary.

        Reading only ``brand_logo_url`` - the layout's internal name - is why
        two send paths carried a school's mark and twenty-nine did not, while
        every stored document already had somewhere to put one.
        """
        for key in ("issuer_logo_url", "school_logo_url",
                    "entity_logo_url", "tenant_logo_url", "brand_logo_url"):
            with self.subTest(key=key):
                self.assertEqual(brand_logo_from_context({key: LOGO}), LOGO)

    def test_the_sender_name_wins_in_the_same_order_as_its_logo(self):
        """Both families are consulted in one order, so the mark and the name
        cannot end up describing different schools."""
        context = {
            "issuer_logo_url": LOGO,
            "school_logo_url": "https://api.codexng.com/v1/i/public/schools/other/logo/",
        }
        self.assertEqual(brand_logo_from_context(context), LOGO)

    def test_a_value_that_cannot_load_is_refused_rather_than_written(self):
        for value in (
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "/v1/i/public/schools/x/logo/",
            "//evil.example/logo.png",
            "HTTP-but-not-really://x",
        ):
            with self.subTest(value=value):
                self.assertEqual(brand_logo_from_context({"school_logo_url": value}), "")

    def test_render_puts_the_resolved_logo_where_the_document_reads_it(self):
        """The render service resolves the family into the one key the stored
        markup names, the way it already does for the sender's name."""
        from vs_notifications.services.render import render_notification_template

        template = NotificationTemplate.objects.filter(
            channel="email", html_is_custom=False,
        ).first()
        if template is None:
            self.skipTest("no platform-composed email template is seeded")

        _, _, html = render_notification_template(
            template, {"school_name": "Holy Cross", "school_logo_url": LOGO},
        )
        self.assertIn(LOGO, mark_cell(html))

        _, _, refused = render_notification_template(
            template, {"school_name": "Holy Cross", "school_logo_url": "javascript:alert(1)"},
        )
        self.assertNotIn("javascript:", refused)
        self.assertIn("CV", mark_cell(refused))
