"""
Tests for vs_rbac.validators: PermissionDependencyValidator and validate_role_permissions.
"""
from django.core.exceptions import ValidationError
from django.test import TestCase

from vs_rbac.validators import PermissionDependencyValidator, validate_role_permissions
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
        make_permission("a.base")
        make_permission("a.mid")
        make_permission("a.top")
        make_dependency("a.mid", "a.base")
        make_dependency("a.top", "a.mid")

        validator = PermissionDependencyValidator()

        # Missing transitive dep
        result = validator.validate_permission_set(["a.top", "a.mid"])
        self.assertFalse(result["valid"])
        self.assertIn("a.mid", result["missing_dependencies"])

        # All satisfied
        result = validator.validate_permission_set(["a.top", "a.mid", "a.base"])
        self.assertTrue(result["valid"])

    def test_circular_dependency_detected(self):
        make_permission("x.a")
        make_permission("x.b")
        make_dependency("x.a", "x.b")
        make_dependency("x.b", "x.a")

        validator = PermissionDependencyValidator()
        result = validator.validate_permission_set(["x.a", "x.b"])
        self.assertFalse(result["valid"])
        self.assertTrue(len(result["errors"]) > 0)

    def test_detect_circular_dependencies(self):
        make_permission("c.a")
        make_permission("c.b")
        make_dependency("c.a", "c.b")
        make_dependency("c.b", "c.a")

        validator = PermissionDependencyValidator()
        errors = validator.detect_circular_dependencies()
        self.assertTrue(len(errors) > 0)

    def test_get_dependencies(self):
        make_permission("d.view")
        make_permission("d.edit")
        make_permission("d.delete")
        make_dependency("d.edit", "d.view")
        make_dependency("d.delete", "d.view")

        validator = PermissionDependencyValidator()
        deps = validator.get_dependencies("d.edit")
        self.assertEqual(deps, {"d.view"})

    def test_get_all_dependencies(self):
        make_permission("e.a")
        make_permission("e.b")
        make_permission("e.c")
        make_dependency("e.c", "e.b")
        make_dependency("e.b", "e.a")

        validator = PermissionDependencyValidator()
        all_deps = validator.get_all_dependencies("e.c")
        self.assertEqual(all_deps, {"e.a", "e.b"})

    def test_empty_set_validates(self):
        validator = PermissionDependencyValidator()
        result = validator.validate_permission_set([])
        self.assertTrue(result["valid"])


class ValidateRolePermissionsTests(TestCase):
    def test_valid_set_passes(self):
        make_permission("f.view")
        make_permission("f.edit")
        make_dependency("f.edit", "f.view")
        # Should not raise
        validate_role_permissions(["f.view", "f.edit"])

    def test_missing_dependency_raises(self):
        make_permission("g.view")
        make_permission("g.edit")
        make_dependency("g.edit", "g.view")
        with self.assertRaises(ValidationError) as ctx:
            validate_role_permissions(["g.edit"])
        self.assertIn("permission_keys", ctx.exception.message_dict)

    def test_empty_set_passes(self):
        validate_role_permissions([])
