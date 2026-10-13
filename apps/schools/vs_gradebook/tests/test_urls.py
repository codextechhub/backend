"""Every gradebook route is mounted, named, documented and guarded by a key."""
from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import get_resolver, resolve, reverse

from ..views.classes import (
    ApprovalsView,
    BroadsheetView,
    ClassReturnView,
    ClassSubmitView,
    CompileView,
    PrincipalCommentsView,
    PublishPreviewView,
    PublishView,
)
from ..views.holds import HoldLiftView, HoldsView
from ..views.hubs import AdminHubView, TeacherHubView
from ..views.moderation import (
    ModerationQueueView,
    SheetApproveView,
    SheetReturnView,
)
from ..views.results import (
    ClassCardsView,
    ReleaseListView,
    ReportCardView,
    StudentResultsView,
)
from ..views.settings import SettingsView
from ..views.sheets import SheetRemindView, SheetScoresView, SheetSubmitView, SheetView
from ..views.setup import (
    RemarkTemplatesView,
    ScaleActivateView,
    ScaleCopyView,
    ScaleDetailView,
    ScaleListCreateView,
    StructureActivateView,
    StructureCopyView,
    StructureDetailView,
    StructureListCreateView,
)

ROUTES = {
    "gradebook-structures": (StructureListCreateView, "/v1/gradebook/structures/", {}),
    "gradebook-structure": (StructureDetailView, "/v1/gradebook/structures/5/", {"pk": 5}),
    "gradebook-structure-copy": (StructureCopyView, "/v1/gradebook/structures/5/copy/", {"pk": 5}),
    "gradebook-structure-activate": (
        StructureActivateView, "/v1/gradebook/structures/5/activate/", {"pk": 5},
    ),
    "gradebook-scales": (ScaleListCreateView, "/v1/gradebook/scales/", {}),
    "gradebook-scale": (ScaleDetailView, "/v1/gradebook/scales/5/", {"pk": 5}),
    "gradebook-scale-copy": (ScaleCopyView, "/v1/gradebook/scales/5/copy/", {"pk": 5}),
    "gradebook-scale-activate": (ScaleActivateView, "/v1/gradebook/scales/5/activate/", {"pk": 5}),
    "gradebook-remark-templates": (RemarkTemplatesView, "/v1/gradebook/remark-templates/", {}),
    "gradebook-settings": (SettingsView, "/v1/gradebook/settings/", {}),
    "gradebook-hub": (AdminHubView, "/v1/gradebook/hub/", {}),
    "gradebook-hub-mine": (TeacherHubView, "/v1/gradebook/hub/mine/", {}),
    "gradebook-moderation": (ModerationQueueView, "/v1/gradebook/moderation/", {}),
    "gradebook-sheet": (SheetView, "/v1/gradebook/sheets/3/7/", {"class_id": 3, "subject_id": 7}),
    "gradebook-sheet-scores": (
        SheetScoresView, "/v1/gradebook/sheets/3/7/scores/", {"class_id": 3, "subject_id": 7},
    ),
    "gradebook-sheet-submit": (
        SheetSubmitView, "/v1/gradebook/sheets/3/7/submit/", {"class_id": 3, "subject_id": 7},
    ),
    "gradebook-sheet-approve": (
        SheetApproveView, "/v1/gradebook/sheets/3/7/approve/", {"class_id": 3, "subject_id": 7},
    ),
    "gradebook-sheet-return": (
        SheetReturnView, "/v1/gradebook/sheets/3/7/return/", {"class_id": 3, "subject_id": 7},
    ),
    "gradebook-sheet-remind": (
        SheetRemindView, "/v1/gradebook/sheets/3/7/remind/", {"class_id": 3, "subject_id": 7},
    ),
    "gradebook-broadsheet": (
        BroadsheetView, "/v1/gradebook/classes/3/broadsheet/", {"class_id": 3},
    ),
    "gradebook-compile": (
        CompileView, "/v1/gradebook/classes/3/students/9/compile/",
        {"class_id": 3, "student_id": 9},
    ),
    "gradebook-class-submit": (
        ClassSubmitView, "/v1/gradebook/classes/3/submit/", {"class_id": 3},
    ),
    "gradebook-class-return": (
        ClassReturnView, "/v1/gradebook/classes/3/return/", {"class_id": 3},
    ),
    "gradebook-principal-comments": (
        PrincipalCommentsView, "/v1/gradebook/classes/3/principal-comments/", {"class_id": 3},
    ),
    "gradebook-publish-preview": (
        PublishPreviewView, "/v1/gradebook/classes/3/publish/preview/", {"class_id": 3},
    ),
    "gradebook-publish": (PublishView, "/v1/gradebook/classes/publish/", {}),
    "gradebook-approvals": (ApprovalsView, "/v1/gradebook/approvals/", {}),
    "gradebook-holds": (HoldsView, "/v1/gradebook/holds/", {}),
    "gradebook-hold-lift": (HoldLiftView, "/v1/gradebook/holds/4/lift/", {"hold_id": 4}),
    "gradebook-release": (ReleaseListView, "/v1/gradebook/release/3/", {"class_id": 3}),
    "gradebook-report-cards": (ClassCardsView, "/v1/gradebook/report-cards/3/", {"class_id": 3}),
    "gradebook-report-card": (
        ReportCardView, "/v1/gradebook/report-cards/3/9/", {"class_id": 3, "student_id": 9},
    ),
    "gradebook-student-results": (
        StudentResultsView, "/v1/gradebook/students/9/results/", {"student_id": 9},
    ),
}
VIEWS = {view for view, _, _ in ROUTES.values()}


def _walk(pattern):
    if hasattr(pattern, "url_patterns"):
        for child in pattern.url_patterns:
            yield from _walk(child)
    elif hasattr(pattern, "callback"):
        yield pattern


class MountTests(SimpleTestCase):
    def test_every_route_is_mounted_under_v1_gradebook(self):
        for name, (view, path, kwargs) in ROUTES.items():
            with self.subTest(route=name):
                self.assertEqual(reverse(name, kwargs=kwargs), path)
                self.assertIs(resolve(path).func.cls, view)

    def test_every_view_of_the_module_is_routed(self):
        routed = {
            entry.callback.cls
            for pattern in get_resolver().url_patterns
            for entry in _walk(pattern)
            if getattr(entry.callback, "cls", None) is not None
            and entry.callback.cls.__module__.startswith("schools.vs_gradebook")
        }
        self.assertEqual(routed, VIEWS)

    def test_no_view_joins_the_pending_tenant_surface(self):
        for view in VIEWS:
            with self.subTest(view=view.__name__):
                self.assertFalse(getattr(view, "pending_tenant_surface", False))

    def test_every_view_names_the_permission_it_demands_and_is_documented(self):
        for view in VIEWS:
            with self.subTest(view=view.__name__):
                self.assertTrue(view.rbac_permission)
                self.assertIn("docstring-name:", view.__doc__)
