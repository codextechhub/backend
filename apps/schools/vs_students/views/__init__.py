from .base import StudentsViewMixin
from .branch_move import BranchMovePreviewView, BranchMoveView
from .guardians import (
    GuardianDetailView,
    GuardianDirectoryView,
    GuardianSearchView,
    GuardianPhotoView,
    GuardianRulesView,
    GuardianStudentsView,
    StudentGuardianDetailView,
    StudentGuardiansView,
)
from .movements import (
    AssignClassView,
    BulkAssignClassView,
    BulkStatusView,
    ChangeStatusView,
    ClassHistoryView,
    ConfirmApplicantView,
    ReactivateStudentView,
    RejectApplicantView,
    StageMoveView,
    StatusHistoryView,
    SuspendStudentView,
    TransferOutView,
    WithdrawStudentView,
)
from .promotion import (
    PromotionBatchView,
    PromotionPreviewView,
    PromotionRulesView,
    PromotionRunView,
)
from .records import (
    AdmissionPolicyView,
    AdmissionRulesView,
    ClassRosterView,
    ClassSeatsView,
    EnrolmentRulesView,
    StudentDocumentDetailView,
    StudentDocumentsView,
    StudentHistoryView,
    StudentSubjectsView,
)
from .students import (
    StudentDetailView,
    StudentListCreateView,
    StudentSearchView,
    StudentSummaryView,
    UnplacedStudentsView,
)

__all__ = [
    "AdmissionPolicyView", "AdmissionRulesView", "AssignClassView",
    "BranchMovePreviewView", "BranchMoveView", "BulkAssignClassView",
    "BulkStatusView", "ChangeStatusView", "ClassHistoryView", "ClassRosterView",
    "ConfirmApplicantView", "EnrolmentRulesView", "GuardianDetailView",
    "GuardianDirectoryView", "GuardianRulesView", "GuardianStudentsView",
    "PromotionBatchView", "PromotionPreviewView", "PromotionRulesView",
    "PromotionRunView", "ReactivateStudentView", "RejectApplicantView",
    "StageMoveView", "StatusHistoryView", "StudentDetailView",
    "StudentDocumentDetailView",
    "StudentDocumentsView", "StudentGuardianDetailView", "StudentGuardiansView",
    "StudentHistoryView", "StudentListCreateView", "StudentSearchView",
    "StudentSubjectsView", "StudentSummaryView", "StudentsViewMixin",
    "SuspendStudentView", "TransferOutView", "UnplacedStudentsView",
    "WithdrawStudentView",
]
