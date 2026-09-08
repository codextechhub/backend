"""The loose matcher every people-directory searches through.

Three modules depend on it now, so the behaviour is pinned here rather than
three times over in their own suites. What each test guards is named in its
docstring, because the interesting cases are the ones that look like noise
until you know which real complaint produced them.
"""
from __future__ import annotations

from django.test import TestCase

from core.search import search
from vs_user.models import User
from vs_rbac.tests.helpers import make_school


class LooseSearchTests(TestCase):
    """Run against ``User``, which every tenant has and which carries names."""

    FIELDS = ("first_name", "last_name", "email", "phone")

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="loose-search", name="Loose Search School")
        cls.tenant = cls.school.tenant
        cls.people = {}
        # One of them carries no phone number, which is the NULL the join has
        # to survive - see test_a_missing_field_does_not_break_the_join.
        for first, last, phone in (
            ("Sunday", "Ekpo", "0803 555 0001"),
            ("Samuel", "Adeyemo", "0803 555 0002"),
            ("Ibrahim", "Sule", ""),
            ("Adaeze", "Nwankwo", None),
        ):
            cls.people[last] = User.objects.create_user(
                email=f"{first}.{last}@loose.test".lower(),
                password="LooseSearch@2026", first_name=first,
                last_name=last, phone=phone, tenant=cls.tenant,
                status="ACTIVE",
            )

    def found(self, query):
        rows = search(
            User.objects.filter(tenant=self.tenant), query,
            fields=self.FIELDS, then=("first_name",),
        )
        return [f"{row.first_name} {row.last_name}" for row in rows]

    def test_a_full_name_finds_the_person(self):
        """The defect this helper exists for.

        Every directory matched each field against the WHOLE query, so the
        thing a reader tries first returned nothing: "Sunday Ekpo" is in no
        single column.
        """
        self.assertEqual(self.found("Sunday Ekpo"), ["Sunday Ekpo"])

    def test_the_order_of_the_names_does_not_matter(self):
        """A reader who types the surname first is not wrong."""
        self.assertEqual(self.found("ekpo sunday"), ["Sunday Ekpo"])

    def test_a_couple_of_letters_from_each_name_is_enough(self):
        self.assertEqual(self.found("su ek"), ["Sunday Ekpo"])

    def test_a_short_guess_reaches_the_person_it_was_a_guess_at(self):
        """The loosest pass, and what it costs.

        "sue" is Su-nday Ekpo, and it is also Sam-u-el Ad-e-yemo and Ibrahim
        S-u-l-e. All three are honest subsequence hits and none is a better one
        than the others, so the guess returns candidates rather than an answer -
        which is what a three-letter query is.
        """
        hits = self.found("sue")
        self.assertIn("Sunday Ekpo", hits)
        self.assertIn("Samuel Adeyemo", hits)

    def test_a_real_match_outranks_a_coincidence(self):
        """What the ranking is actually for.

        "sul" starts a word in Ibrahim Sule and merely threads through Sam-u-e
        ... no: it reaches Samuel Adeyemo only as a subsequence. The one whose
        name begins that way has to come first, or a loose pass would bury the
        person somebody was plainly typing.
        """
        hits = self.found("sul")
        self.assertEqual(hits[0], "Ibrahim Sule")

    def test_a_missing_field_does_not_break_the_join(self):
        """Concatenating a NULL in Postgres yields NULL.

        Without coalescing, one person with no phone number would have an empty
        searchable string and drop out of every search - including a search for
        their own name, which is not a phone number at all.
        """
        self.assertEqual(self.found("Ibrahim"), ["Ibrahim Sule"])
        self.assertEqual(self.found("Adaeze Nwankwo"), ["Adaeze Nwankwo"])

    def test_every_named_field_is_searchable(self):
        self.assertEqual(self.found("0803 555 0001"), ["Sunday Ekpo"])

    def test_an_empty_query_narrows_nothing(self):
        """Not "matches nothing": a blank box is not a filter.

        The helper returns the queryset untouched, so a screen that always
        passes its search box through gets the whole list when it is empty.
        """
        self.assertEqual(len(self.found("")), 4)
        self.assertEqual(len(self.found("   ")), 4)

    def test_a_query_that_matches_nobody_returns_nobody(self):
        self.assertEqual(self.found("Wolverhampton"), [])

    def test_regex_characters_are_matched_literally(self):
        """A search box takes whatever somebody types.

        Without escaping, ".*" is a pattern that matches everybody and "(" is
        an unbalanced group the database refuses outright - so a stray bracket
        would turn a search into a 500.
        """
        self.assertEqual(self.found(".*"), [])
        self.assertEqual(self.found("("), [])
        self.assertEqual(self.found("Sunday("), [])
