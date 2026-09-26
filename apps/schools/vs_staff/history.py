"""The staff rows whose history a staff profile can be read at.

The record, the qualifications, the documents, the teaching assignments and
the leave requests each keep a history here, so the profile and every tab on it
can be rebuilt as they stood on an earlier day. The branch postings are part of
the record's own history, kept as a list of branch ids. Employment events are
dated rows of their own and need none.

The person's name, phone, email and gender live on their account, whose
history ``vs_user`` keeps.
"""
from vs_history.registry import track

STAFF = "vs_staff.staffprofile"


def register():
    """Declare the tracked staff-side models to the history engine."""
    from .models import LeaveRequest, StaffDocument, StaffProfile, StaffQualification, TeachingAssignment

    track(StaffProfile, m2m=("additional_postings",))
    for model in (StaffQualification, StaffDocument, TeachingAssignment, LeaveRequest):
        track(model, owners=lambda row: [(STAFF, row.staff_id)])
