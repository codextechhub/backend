"""Official templates installed for organogram bulk upload."""

from django.test import TestCase

from .models import ImportTemplate


class OrganogramTemplateTests(TestCase):
    def test_each_organogram_dataset_has_an_active_downloadable_template(self):
        templates = {
            template.dataset_type: template
            for template in ImportTemplate.objects.filter(
                dataset_type__in=("org_units", "positions", "matrix_reports"),
            ).prefetch_related("columns")
        }

        self.assertEqual(set(templates), {"org_units", "positions", "matrix_reports"})
        self.assertEqual(
            {dataset_type: template.columns.count() for dataset_type, template in templates.items()},
            {"org_units": 6, "positions": 7, "matrix_reports": 3},
        )
        self.assertTrue(all(template.status == "active" for template in templates.values()))
        self.assertTrue(all(template.is_download_enabled for template in templates.values()))
