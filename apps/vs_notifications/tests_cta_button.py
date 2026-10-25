"""A template's button disappears when its destination renders empty.

An invoice email's "Pay online" button points at ``{{ payment_link }}``. A
customer the books' owner asks to pay by bank transfer has no link, so the same
stored document must render without a button rather than with one that goes
nowhere.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from .services.layout import EMAIL_BRAND_PLACEHOLDER, compose_email_html
from .services.render import render_template


def stored(cta_url):
    return compose_email_html(
        subject="Invoice", body="Hello.\n\n{{ instructions }}", cta_label="Pay online",
        cta_url=cta_url, brand=EMAIL_BRAND_PLACEHOLDER, as_template=True,
    )


class ButtonFollowsItsDestinationTests(SimpleTestCase):
    def test_a_variable_destination_shows_the_button_only_when_it_has_a_value(self):
        html = stored("{{ payment_link }}")
        full = render_template(html, {"payment_link": "https://pay.example/abc", "email_brand": "XVS"}, autoescape=True)
        empty = render_template(html, {"payment_link": "", "email_brand": "XVS"}, autoescape=True)
        self.assertIn("https://pay.example/abc", full)
        self.assertIn("Pay online", full)
        self.assertNotIn("Pay online", empty)
        self.assertNotIn("Button not working", empty)

    def test_a_destination_that_is_not_one_plain_variable_is_left_alone(self):
        html = stored("{{ base }}/pay/{{ token }}")
        self.assertIn("Pay online", html)
        self.assertNotIn("{% if base", html)
        self.assertNotIn("{% if token", html)

    def test_a_rendered_message_with_a_literal_destination_is_unchanged(self):
        html = compose_email_html(
            subject="Invoice", body="Hello.", cta_label="Pay online",
            cta_url="https://pay.example/abc", brand="XVS",
        )
        self.assertIn('href="https://pay.example/abc"', html)
        self.assertNotIn("{% if", html)
