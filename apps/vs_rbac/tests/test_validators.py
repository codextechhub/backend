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
