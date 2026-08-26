from .sessions import (  # noqa: F401
    SessionActivateView,
    SessionArchiveView,
    SessionRollForwardView,
    SessionDetailView,
    SessionListCreateView,
    TermDetailView,
    TermListCreateView,
)
from .structure import (  # noqa: F401
    DepartmentArchiveView,
    DepartmentDetailView,
    DepartmentRestoreView,
    DepartmentListCreateView,
    LevelBulkCreateView,
    LevelArchiveView,
    LevelDetailView,
    LevelRestoreView,
    LevelListCreateView,
    ProgramArchiveView,
    ProgramDetailView,
    ProgramRestoreView,
    ProgramListCreateView,
)
from .classes import (  # noqa: F401
    ClassArchiveView,
    ClassDetailView,
    ClassListCreateView,
    ClassRestoreView,
    GenerateArmsView,
    SubjectArchiveView,
    SubjectDetailView,
    SubjectRestoreView,
    SubjectListCreateView,
    SubjectOfferingsView,
)
from .reads import (  # noqa: F401
    OverviewView,
    StructureTreeView,
)
