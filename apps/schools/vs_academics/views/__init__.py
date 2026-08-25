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
    DepartmentDetailView,
    DepartmentListCreateView,
    LevelBulkCreateView,
    LevelDetailView,
    LevelListCreateView,
    ProgramDetailView,
    ProgramListCreateView,
)
from .classes import (  # noqa: F401
    ClassArchiveView,
    ClassDetailView,
    ClassListCreateView,
    ClassRestoreView,
    GenerateArmsView,
    SubjectDetailView,
    SubjectListCreateView,
    SubjectOfferingsView,
)
from .reads import (  # noqa: F401
    OverviewView,
    StructureTreeView,
)
