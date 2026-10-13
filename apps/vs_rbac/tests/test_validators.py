"""
Tests for vs_rbac.validators: PermissionDependencyValidator and validate_role_permissions.
"""
import re

from django.core.exceptions import ValidationError
from django.test import TestCase

from vs_rbac.validators import (
    PermissionDependencyValidator,
    describe_missing_dependencies,
    validate_role_permissions,
)
from .helpers import make_permission, make_dependency


class PermissionDependencyValidatorTests(TestCase):
    def test_no_dependencies_validates(self):
        make_permission("finance.invoice.view")
        validator = PermissionDependencyValidator()
        result = validator.validate_permission_set(["finance.invoice.view"])
        self.assertTrue(result["valid"])
        self.assertEqual(result["missing_dependencies"], {})

    def test_satisfied_dependency_validates(self):
        make_permission("finance.invoice.view")
        make_permission("finance.invoice.approve")
        make_dependency("finance.invoice.approve", "finance.invoice.view")

        validator = PermissionDependencyValidator()
        result = validator.validate_permission_set([
            "finance.invoice.view",
            "finance.invoice.approve",
        ])
        self.assertTrue(result["valid"])

    def test_missing_dependency_fails(self):
        make_permission("finance.invoice.view")
        make_permission("finance.invoice.approve")
        make_dependency("finance.invoice.approve", "finance.invoice.view")

        validator = PermissionDependencyValidator()
        result = validator.validate_permission_set(["finance.invoice.approve"])
        self.assertFalse(result["valid"])
        self.assertIn("finance.invoice.approve", result["missing_dependencies"])
        self.assertIn(
            "finance.invoice.view",
            result["missing_dependencies"]["finance.invoice.approve"],
        )

    def test_transitive_dependency(self):
        make_permission("a.res.base")
        make_permission("a.res.mid")
        make_permission("a.res.top")
        make_dependency("a.res.mid", "a.res.base")
        make_dependency("a.res.top", "a.res.mid")

        validator = PermissionDependencyValidator()

        # Missing transitive dep
        result = validator.validate_permission_set(["a.res.top", "a.res.mid"])
        self.assertFalse(result["valid"])
        self.assertIn("a.res.mid", result["missing_dependencies"])

        # All satisfied
        result = validator.validate_permission_set(["a.res.top", "a.res.mid", "a.res.base"])
        self.assertTrue(result["valid"])

    def test_circular_dependency_detected(self):
        make_permission("x.res.a")
        make_permission("x.res.b")
        make_dependency("x.res.a", "x.res.b")
        make_dependency("x.res.b", "x.res.a")

        validator = PermissionDependencyValidator()
        result = validator.validate_permission_set(["x.res.a", "x.res.b"])
        self.assertFalse(result["valid"])
        self.assertTrue(len(result["errors"]) > 0)

    def test_detect_circular_dependencies(self):
        make_permission("c.res.a")
        make_permission("c.res.b")
        make_dependency("c.res.a", "c.res.b")
        make_dependency("c.res.b", "c.res.a")

        validator = PermissionDependencyValidator()
        errors = validator.detect_circular_dependencies()
        self.assertTrue(len(errors) > 0)

    def test_get_dependencies(self):
        make_permission("d.res.view")
        make_permission("d.res.edit")
        make_permission("d.res.delete")
        make_dependency("d.res.edit", "d.res.view")
        make_dependency("d.res.delete", "d.res.view")

        validator = PermissionDependencyValidator()
        deps = validator.get_dependencies("d.res.edit")
        self.assertEqual(deps, {"d.res.view"})

    def test_get_all_dependencies(self):
        make_permission("e.res.a")
        make_permission("e.res.b")
        make_permission("e.res.c")
        make_dependency("e.res.c", "e.res.b")
        make_dependency("e.res.b", "e.res.a")

        validator = PermissionDependencyValidator()
        all_deps = validator.get_all_dependencies("e.res.c")
        self.assertEqual(all_deps, {"e.res.a", "e.res.b"})

    def test_empty_set_validates(self):
        validator = PermissionDependencyValidator()
        result = validator.validate_permission_set([])
        self.assertTrue(result["valid"])


class ValidateRolePermissionsTests(TestCase):
    def test_valid_set_passes(self):
        make_permission("f.res.view")
        make_permission("f.res.edit")
        make_dependency("f.res.edit", "f.res.view")
        # Should not raise
        validate_role_permissions(["f.res.view", "f.res.edit"])

    def test_missing_dependency_raises(self):
        make_permission("g.res.view")
        make_permission("g.res.edit")
        make_dependency("g.res.edit", "g.res.view")
        with self.assertRaises(ValidationError) as ctx:
            validate_role_permissions(["g.res.edit"])
        self.assertIn("permission_keys", ctx.exception.message_dict)

    def test_empty_set_passes(self):
        validate_role_permissions([])


RAW_KEY = re.compile(r"\b[a-z_]+\.[a-z_]+\.[a-z_]+\b")


class DependencyWordingTests(TestCase):
    """Missing-dependency messages name permissions the way the role screens do."""

    @classmethod
    def setUpTestData(cls):
        make_permission("school.students.view")
        make_permission("school.students.transfer")
        make_permission("school.students.export", description="Download the student list.")
        make_permission("school.students.update")
        make_permission("school.classes.view")
        make_dependency("school.students.transfer", "school.students.view")
        make_dependency("school.students.export", "school.students.view")
        make_dependency("school.students.update", "school.students.view")
        make_dependency("school.students.update", "school.classes.view")

    def test_blank_descriptions_read_as_action_and_resource(self):
        sentences = describe_missing_dependencies(
            {"school.students.transfer": ["school.students.view"]},
        )

        self.assertEqual(sentences, ['"Transfer students" also needs "View students".'])

    def test_a_described_permission_uses_its_description(self):
        sentences = describe_missing_dependencies(
            {"school.students.export": ["school.students.view"]},
        )

        self.assertEqual(
            sentences, ['"Download the student list" also needs "View students".'],
        )

    def test_two_dependencies_join_with_and(self):
        sentences = describe_missing_dependencies(
            {"school.students.update": ["school.classes.view", "school.students.view"]},
        )

        self.assertEqual(
            sentences,
            ['"Update students" also needs "View classes" and "View students".'],
        )

    def test_three_dependencies_join_with_commas_and_and(self):
        make_permission("school.students.delete")
        make_dependency("school.students.delete", "school.students.view")
        make_dependency("school.students.delete", "school.students.update")
        make_dependency("school.students.delete", "school.classes.view")

        sentences = describe_missing_dependencies({
            "school.students.delete": [
                "school.classes.view", "school.students.update", "school.students.view",
            ],
        })

        self.assertEqual(
            sentences,
            ['"Delete students" also needs "View classes", "Update students" '
             'and "View students".'],
        )

    def test_sentences_are_sorted_by_dependent_key(self):
        sentences = describe_missing_dependencies({
            "school.students.update": ["school.students.view"],
            "school.students.transfer": ["school.students.view"],
        })

        self.assertEqual(
            sentences,
            ['"Transfer students" also needs "View students".',
             '"Update students" also needs "View students".'],
        )

    def test_a_key_without_a_row_falls_back_to_the_key(self):
        sentences = describe_missing_dependencies({"gone.thing.do": ["gone.thing.see"]})

        self.assertEqual(sentences, ['"gone.thing.do" also needs "gone.thing.see".'])

    def test_all_labels_load_in_one_query(self):
        with self.assertNumQueries(1):
            describe_missing_dependencies({
                "school.students.update": ["school.classes.view", "school.students.view"],
                "school.students.transfer": ["school.students.view"],
            })

    def test_no_missing_dependencies_gives_no_sentences(self):
        with self.assertNumQueries(0):
            self.assertEqual(describe_missing_dependencies({}), [])

    def test_role_error_carries_readable_wording_and_no_raw_key(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_role_permissions(["school.students.transfer"])

        messages = ctx.exception.message_dict["permission_keys"]
        self.assertEqual(
            messages, ['"Transfer students" also needs "View students".'],
        )
        self.assertIsNone(RAW_KEY.search(" ".join(messages)))

    def test_permission_group_error_carries_readable_wording_and_no_raw_key(self):
        from vs_rbac.serializers.registry import PermissionGroupDetailSerializer

        serializer = PermissionGroupDetailSerializer(data={
            "name": "Transfer desk",
            "permission_keys": ["school.students.transfer"],
        })

        self.assertFalse(serializer.is_valid())
        message = " ".join(str(m) for m in serializer.errors["permission_keys"])
        self.assertEqual(
            message,
            'Include every required permission in the group: '
            '"Transfer students" also needs "View students".',
        )
        self.assertIsNone(RAW_KEY.search(message))
