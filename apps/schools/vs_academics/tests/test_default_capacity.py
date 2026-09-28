"""A new class takes the school's default class size when it is given none.

The default is a student setting (``students.capacity.default``) because
capacity is what placement checks, and it reaches the two places a class is
created by hand: the class form and "generate arms". A school that has set
nothing gets classes with no limit, as it always did.
"""
from __future__ import annotations

from schools.vs_academics.models import SchoolClass

from .test_class_subject_endpoints import _Base


class DefaultCapacityTests(_Base):

    def set_default(self, tenant, seats):
        from schools.vs_students.services.rules import read_rules, write_rules

        current = read_rules(tenant)
        write_rules(
            tenant, self.admin,
            min_age_years=current.min_age_years,
            max_age_years=current.max_age_years,
            required_documents=current.required_documents,
            required_fields=current.required_fields,
            capacity_mode=current.capacity_mode,
            default_capacity=seats,
        )

    def test_with_no_default_a_new_class_has_no_limit(self):
        response = self.post(
            self.admin, "academics-class-list",
            {"name": "JSS1 A", "level": self.jss1.pk},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(SchoolClass.all_objects.get(name="JSS1 A").capacity)

    def test_a_class_created_without_a_capacity_takes_the_default(self):
        self.set_default(self.tenant, 35)
        response = self.post(
            self.admin, "academics-class-list",
            {"name": "JSS1 A", "level": self.jss1.pk},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["capacity"], 35)

    def test_a_capacity_given_on_the_form_wins(self):
        self.set_default(self.tenant, 35)
        response = self.post(
            self.admin, "academics-class-list",
            {"name": "JSS1 A", "level": self.jss1.pk, "capacity": 20},
        )
        self.assertEqual(response.data["data"]["capacity"], 20)

    def test_generated_arms_take_the_default(self):
        self.set_default(self.tenant, 40)
        response = self.post(
            self.admin, "academics-class-arms",
            {"level": self.jss1.pk, "arms": ["A", "B"], "branch": self.ikeja.id},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual([r["capacity"] for r in response.data["data"]], [40, 40])

    def test_another_schools_default_is_not_this_schools(self):
        """Sunrise's forty-five seats never reach a Brightfield class."""
        self.set_default(self.other.tenant, 45)
        response = self.post(
            self.admin, "academics-class-arms",
            {"level": self.jss1.pk, "arms": ["A"]},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["data"][0]["capacity"])

    def test_editing_a_class_never_applies_the_default(self):
        klass = SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.jss1, session=self.year,
            name="JSS1 A", code="JSS1A",
        )
        self.set_default(self.tenant, 35)
        from django.urls import reverse

        url = reverse("academics-class-detail", kwargs={"pk": klass.pk})
        response = self.client_for(self.admin).patch(
            f"{url}?tenant={self.tenant.slug}", {"description": "Morning"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        klass.refresh_from_db()
        self.assertIsNone(klass.capacity)
