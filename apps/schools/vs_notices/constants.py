"""Words, keys and limits the notices module shares."""
from django.db import models

#: Reading the school's notices on a staff screen.
PERM_VIEW = "school.notices.view"
#: Posting, correcting and withdrawing a notice to any audience the holder's
#: branch reach covers.
PERM_POST = "school.notices.post"
#: Posting to the classes the holder teaches this year, and correcting or
#: withdrawing their own such notices. A teacher's key.
PERM_POST_CLASS = "school.class_notices.post"

#: The in-app and email notice a pupil receives when a notice reaches them.
EVENT_PUBLISHED = "notices.published"

TITLE_MAX = 150
BODY_MAX = 5000
#: Audience rows one notice may carry: enough for every class of a large
#: school's two levels, few enough that the picker stays a picker.
AUDIENCE_MAX = 40


class NoticeStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PUBLISHED = "PUBLISHED", "Published"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"


class AudienceKind(models.TextChoices):
    SCHOOL = "SCHOOL", "Whole school"
    BRANCH = "BRANCH", "Branch"
    LEVEL = "LEVEL", "Level"
    CLASS = "CLASS", "Class"
