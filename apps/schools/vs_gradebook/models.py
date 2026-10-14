"""Assessment structures, grading scales, score sheets and the results built from them.

Every model carries its own ``tenant`` foreign key, even where its parent
already has one, because :class:`vs_rbac.managers.TenantAwareManager` filters on
a model's own ``tenant`` field and returns everything otherwise. Reaching the
tenant through a parent is not scoping.

**Setup is shared configuration.** An :class:`AssessmentStructure` (the
components a result is made of) and a :class:`GradingScale` (the bands a
percentage falls in) belong to a programme. A null ``branch`` means every
branch; a branch-specific one wins for that branch's classes. A programme has at
most one of each IN_USE per branch (and one school-wide), which the database
holds as partial unique constraints. A draft may be incomplete, so nothing
stored forces a structure to total 100 or a scale to start at 0; putting one in
use is where that is checked (:mod:`.services.setup`).

**A class's term is the unit the results hang on** (:class:`ClassTerm`): one
class in one term. It names the branch the class sits in, which is never null
because a result is a transaction in the platform's branch sense. It fixes its
structure the first time a mark is entered, so every sheet of one class shares
its columns. The scale is read live until publication and is then frozen in
each student's snapshot.

**A sheet is one subject in one class-term** (:class:`ScoreSheet`), created on
first write; a missing row reads as not started. A :class:`Score` is one
student's cell for one component: a whole mark, or a marker (ABS, EXC), or
nothing. A blank is the absence of both, never a zero.

**What is derived is never stored.** Totals, percentages, grades, class
statistics, averages and positions are worked out from the scores
(:mod:`.services.compute`). The one exception is the published snapshot on
:class:`StudentTermResult`, which freezes the figures a family was given so a
later change to a scale or a sheet cannot alter a published result.

**History is not deleted.** A retired structure or scale stays because
published results refer to it. A hold is lifted, not removed. A published result
changes only through an :class:`Amendment`, and every snapshot ever issued stays
in :class:`ResultVersion`.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from vs_rbac.managers import TenantAwareManager

from .constants import (
    COMPONENT_MAX_SCORE_MAX,
    COMPONENT_MAX_SCORE_MIN,
    BAND_MIN_SCORE_MAX,
    BAND_MIN_SCORE_MIN,
    AmendmentStatus,
    EmailState,
    Marker,
    SetupStatus,
    SheetStatus,
    StoredStage,
)


class _Owned(models.Model):
    """Managers and timestamps, shared by every model here.

    The ``tenant`` column is deliberately not declared here: one base column
    would mean one ``related_name`` across every model, so it would have to be
    ``"+"``, which disables the reverse accessor entirely.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"


def _user_fk(related_name="+"):
    return models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name=related_name,
    )


# ── Setup ──────────────────────────────────────────────────────────────────

class AssessmentStructure(_Owned):
    """The components a programme's results are made of, such as two CAs and an exam."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="assessment_structures",
    )
    program = models.ForeignKey(
        "vs_academics.Program", on_delete=models.PROTECT,
        related_name="assessment_structures",
    )
    #: Null means every branch of the school.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, null=True, blank=True,
        related_name="assessment_structures",
    )
    name = models.CharField(max_length=80)
    status = models.CharField(
        max_length=10, choices=SetupStatus.choices, default=SetupStatus.DRAFT,
    )
    copied_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="copies",
    )
    created_by = _user_fk()

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "program", "branch"],
                condition=Q(status=SetupStatus.IN_USE, branch__isnull=False),
                name="uq_gradebook_structure_in_use_branch",
            ),
            models.UniqueConstraint(
                fields=["tenant", "program"],
                condition=Q(status=SetupStatus.IN_USE, branch__isnull=True),
                name="uq_gradebook_structure_in_use_school",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "program", "status"])]
        ordering = ["program_id", "id"]

    def __str__(self):
        return self.name


class AssessmentComponent(_Owned):
    """One column of a structure: a name, a whole-number maximum and whether it is the examination."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="assessment_components",
    )
    structure = models.ForeignKey(
        AssessmentStructure, on_delete=models.CASCADE, related_name="components",
    )
    name = models.CharField(max_length=60)
    short_name = models.CharField(max_length=12)
    max_score = models.PositiveSmallIntegerField()
    is_exam = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta(_Owned.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    max_score__gte=COMPONENT_MAX_SCORE_MIN,
                    max_score__lte=COMPONENT_MAX_SCORE_MAX,
                ),
                name="ck_gradebook_component_max_score_range",
            ),
        ]
        indexes = [models.Index(fields=["structure", "order"])]
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.name} ({self.max_score})"


class GradingScale(_Owned):
    """The bands a percentage falls in to become a grade and a remark."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="grading_scales",
    )
    program = models.ForeignKey(
        "vs_academics.Program", on_delete=models.PROTECT, related_name="grading_scales",
    )
    #: Null means every branch of the school.
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, null=True, blank=True,
        related_name="grading_scales",
    )
    name = models.CharField(max_length=80)
    status = models.CharField(
        max_length=10, choices=SetupStatus.choices, default=SetupStatus.DRAFT,
    )
    copied_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="copies",
    )
    created_by = _user_fk()

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "program", "branch"],
                condition=Q(status=SetupStatus.IN_USE, branch__isnull=False),
                name="uq_gradebook_scale_in_use_branch",
            ),
            models.UniqueConstraint(
                fields=["tenant", "program"],
                condition=Q(status=SetupStatus.IN_USE, branch__isnull=True),
                name="uq_gradebook_scale_in_use_school",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "program", "status"])]
        ordering = ["program_id", "id"]

    def __str__(self):
        return self.name


class GradeBand(_Owned):
    """One band of a scale: from its lowest mark up to the next band's."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="grade_bands",
    )
    scale = models.ForeignKey(
        GradingScale, on_delete=models.CASCADE, related_name="bands",
    )
    min_score = models.PositiveSmallIntegerField()
    grade = models.CharField(max_length=10)
    remark = models.CharField(max_length=120, blank=True, default="")
    is_fail = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta(_Owned.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    min_score__gte=BAND_MIN_SCORE_MIN, min_score__lte=BAND_MIN_SCORE_MAX,
                ),
                name="ck_gradebook_band_min_score_range",
            ),
        ]
        indexes = [models.Index(fields=["scale", "order"])]
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.grade} from {self.min_score}"


class RemarkTemplate(_Owned):
    """A sentence the principal can start a class's comment from, for one band of overall average."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="remark_templates",
    )
    band_min = models.PositiveSmallIntegerField()
    text = models.CharField(max_length=300)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "band_min"], name="uq_gradebook_remark_tenant_band",
            ),
            models.CheckConstraint(
                condition=Q(band_min__gte=0, band_min__lte=100),
                name="ck_gradebook_remark_band_range",
            ),
        ]
        ordering = ["-band_min"]

    def __str__(self):
        return f"{self.band_min}+: {self.text}"


# ── A class in a term ──────────────────────────────────────────────────────

class ClassTerm(_Owned):
    """One class in one term: the unit its sheets are compiled, approved and published as.

    ``stage`` holds only the two stages that are decisions: AWAITING (the class
    teacher has submitted the class) and PUBLISHED. Entering and compiling are
    read off the sheets. ``branch`` is the class's branch, or the school's only
    branch where the class names none, and is never null.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="gradebook_class_terms",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.PROTECT,
        related_name="gradebook_terms",
    )
    term = models.ForeignKey(
        "vs_academics.AcademicTerm", on_delete=models.PROTECT,
        related_name="gradebook_class_terms",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="gradebook_class_terms",
    )
    #: Fixed when the first mark of the class's first sheet is entered.
    structure = models.ForeignKey(
        AssessmentStructure, on_delete=models.PROTECT, null=True, blank=True,
        related_name="class_terms",
    )
    stage = models.CharField(
        max_length=10, choices=StoredStage.choices, null=True, blank=True,
    )
    submitted_by = _user_fk()
    submitted_at = models.DateTimeField(null=True, blank=True)
    returned_by = _user_fk()
    returned_at = models.DateTimeField(null=True, blank=True)
    returned_note = models.TextField(blank=True, default="")
    published_by = _user_fk()
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["school_class", "term"], name="uq_gradebook_class_term",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "term"]),
            models.Index(fields=["tenant", "term", "stage"]),
            models.Index(fields=["structure", "stage"]),
        ]

    def __str__(self):
        return f"{self.school_class_id} / {self.term_id}"


class ScoreSheet(_Owned):
    """One subject's scores for one class-term."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="gradebook_sheets",
    )
    class_term = models.ForeignKey(
        ClassTerm, on_delete=models.PROTECT, related_name="sheets",
    )
    subject = models.ForeignKey(
        "vs_academics.Subject", on_delete=models.PROTECT, related_name="gradebook_sheets",
    )
    status = models.CharField(
        max_length=12, choices=SheetStatus.choices, default=SheetStatus.NOT_STARTED,
    )
    #: Decided when the sheet is submitted: the class's programme is one the
    #: school moderates and the subject's department has a head.
    needs_moderation = models.BooleanField(default=False)
    submitted_by = _user_fk()
    submitted_at = models.DateTimeField(null=True, blank=True)
    moderated_by = _user_fk()
    moderated_at = models.DateTimeField(null=True, blank=True)
    returned_by = _user_fk()
    returned_at = models.DateTimeField(null=True, blank=True)
    returned_note = models.TextField(blank=True, default="")
    saved_at = models.DateTimeField(null=True, blank=True)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["class_term", "subject"], name="uq_gradebook_sheet_class_term_subject",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "status"])]

    def __str__(self):
        return f"{self.class_term_id} / {self.subject_id}"


class Score(_Owned):
    """One student's cell for one component on a sheet.

    A cell is a whole ``mark``, or a ``marker`` (ABS or EXC), or neither. A row
    holding both is refused by the database. A blank cell may be stored as a row
    with neither or not stored at all; both read as blank.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="gradebook_scores",
    )
    sheet = models.ForeignKey(
        ScoreSheet, on_delete=models.PROTECT, related_name="scores",
    )
    student = models.ForeignKey(
        "vs_students.Student", on_delete=models.PROTECT, related_name="gradebook_scores",
    )
    #: PROTECT: a component with marks under it cannot be removed.
    component = models.ForeignKey(
        AssessmentComponent, on_delete=models.PROTECT, related_name="scores",
    )
    mark = models.PositiveSmallIntegerField(null=True, blank=True)
    marker = models.CharField(
        max_length=3, choices=Marker.choices, null=True, blank=True,
    )
    entered_by = _user_fk()
    entered_at = models.DateTimeField(default=timezone.now)
    #: Who asked for the correction that last changed this cell after the
    #: result was published, and when it was approved. ``entered_by`` and
    #: ``entered_at`` keep naming the teacher's original entry.
    amended_by = _user_fk()
    amended_at = models.DateTimeField(null=True, blank=True)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["sheet", "student", "component"],
                name="uq_gradebook_score_cell",
            ),
            models.CheckConstraint(
                condition=Q(mark__isnull=True) | Q(marker__isnull=True),
                name="ck_gradebook_score_mark_or_marker",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "student"]),
            models.Index(fields=["component"]),
        ]

    def __str__(self):
        return f"{self.student_id}: {self.marker or self.mark}"


class ScoreImport(_Owned):
    """A spreadsheet of scores that has been checked and is waiting to be confirmed.

    Short-lived by design: it lives for a day (``expires_at``), is used once
    (``confirmed_at``) and holds nothing the sheet does not. It is tied to the
    user who uploaded it, to the sheet it was checked against and to the
    structure that sheet had, so another teacher, another sheet or another set
    of columns cannot confirm it.

    ``fingerprint`` is a digest of every stored cell of the sheet at the time of
    the check. A confirm that finds the sheet's cells digest differently refuses,
    so a confirm never writes over marks entered after the preview.

    ``rows`` is the checked file, row by row: the preview as the caller was
    shown it, plus the cells each clean row would write. It is read back only by
    the confirm, which renders nothing from it but the sheet itself.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.CASCADE, related_name="gradebook_score_imports",
    )
    token = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+",
    )
    school_class = models.ForeignKey(
        "vs_academics.SchoolClass", on_delete=models.CASCADE, related_name="+",
    )
    subject = models.ForeignKey(
        "vs_academics.Subject", on_delete=models.CASCADE, related_name="+",
    )
    term = models.ForeignKey(
        "vs_academics.AcademicTerm", on_delete=models.CASCADE, related_name="+",
    )
    structure = models.ForeignKey(
        AssessmentStructure, on_delete=models.CASCADE, related_name="+",
    )
    fingerprint = models.CharField(max_length=64)
    file_name = models.CharField(max_length=255, blank=True, default="")
    rows = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta(_Owned.Meta):
        indexes = [
            models.Index(fields=["expires_at"]),
            models.Index(fields=["tenant", "user"]),
        ]

    def __str__(self):
        return f"{self.school_class_id} / {self.subject_id} / {self.term_id}"


# ── Results ────────────────────────────────────────────────────────────────

class StudentTermResult(_Owned):
    """One student's result for a class-term: the comment and ratings, and once published the snapshot.

    ``snapshot`` is null until the class is published. Then it holds everything
    the family was given (subject rows with component cells, totals, average,
    position, grade, remark, the scale's id, the principal's comment), so a
    later change to a scale or a sheet changes nothing that was published.
    ``version`` is 0 until the first publication and counts amendments after it.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="gradebook_results",
    )
    class_term = models.ForeignKey(
        ClassTerm, on_delete=models.PROTECT, related_name="results",
    )
    student = models.ForeignKey(
        "vs_students.Student", on_delete=models.PROTECT, related_name="gradebook_results",
    )
    teacher_comment = models.TextField(blank=True, default="")
    comment_by = _user_fk()
    comment_at = models.DateTimeField(null=True, blank=True)
    #: Trait name to a whole rating from 1 to 5.
    ratings = models.JSONField(default=dict, blank=True)
    snapshot = models.JSONField(null=True, blank=True)
    version = models.PositiveSmallIntegerField(default=0)
    released_at = models.DateTimeField(null=True, blank=True)
    email_state = models.CharField(
        max_length=10, choices=EmailState.choices, null=True, blank=True,
    )
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["class_term", "student"], name="uq_gradebook_result_class_term_student",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "student"])]

    def __str__(self):
        return f"{self.student_id} / {self.class_term_id}"


class ResultVersion(_Owned):
    """One snapshot ever issued for a student's result, kept for good.

    Publication writes version 1 for every student; an approved amendment writes
    the next. The current snapshot is also on :class:`StudentTermResult`, so a
    report card reads one row, while this table is the history: an earlier
    version is never overwritten or deleted. ``amendment`` names the correction
    that produced the version and is null for the first publication. One
    correction produces one version of the amended student's own result and one
    of each later term's cumulative result of the same school year, so an
    amendment has at most one version for each result.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="gradebook_result_versions",
    )
    result = models.ForeignKey(
        StudentTermResult, on_delete=models.PROTECT, related_name="versions",
    )
    version = models.PositiveSmallIntegerField()
    snapshot = models.JSONField()
    amendment = models.ForeignKey(
        "Amendment", on_delete=models.PROTECT, null=True, blank=True,
        related_name="result_versions",
    )

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["result", "version"], name="uq_gradebook_result_version",
            ),
            models.UniqueConstraint(
                fields=["amendment", "result"], name="uq_gradebook_result_version_amendment",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "result"])]
        ordering = ["result_id", "version"]

    def __str__(self):
        return f"{self.result_id} v{self.version}"


class Amendment(_Owned):
    """A correction to one subject of a student's published result.

    ``before`` and ``after`` are the subject's cells as a list of
    ``{"component", "mark", "marker"}`` in component order: ``before`` as it was
    published and ``after`` as the correction would leave it. They are kept as
    typed, not derived, so a decided amendment still reads the same years later.
    One amendment waits at a time for each student, subject and term, which the
    database holds as a partial unique constraint over PENDING rows.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="gradebook_amendments",
    )
    class_term = models.ForeignKey(
        ClassTerm, on_delete=models.PROTECT, related_name="amendments",
    )
    student = models.ForeignKey(
        "vs_students.Student", on_delete=models.PROTECT, related_name="gradebook_amendments",
    )
    subject = models.ForeignKey(
        "vs_academics.Subject", on_delete=models.PROTECT, related_name="gradebook_amendments",
    )
    reason = models.TextField()
    before = models.JSONField(default=list)
    after = models.JSONField(default=list)
    status = models.CharField(
        max_length=10, choices=AmendmentStatus.choices, default=AmendmentStatus.PENDING,
    )
    raised_by = _user_fk()
    raised_at = models.DateTimeField(default=timezone.now)
    decided_by = _user_fk()
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["class_term", "student", "subject"],
                condition=Q(status=AmendmentStatus.PENDING),
                name="uq_gradebook_amendment_pending",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status", "raised_at"]),
            models.Index(fields=["class_term", "student"]),
            models.Index(fields=["tenant", "student"]),
        ]

    def __str__(self):
        return f"{self.student_id} / {self.subject_id} ({self.status})"


class PrincipalComment(_Owned):
    """The principal's comment for one band of overall average in a class-term."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="gradebook_principal_comments",
    )
    class_term = models.ForeignKey(
        ClassTerm, on_delete=models.PROTECT, related_name="principal_comments",
    )
    band_min = models.PositiveSmallIntegerField()
    text = models.TextField()
    written_by = _user_fk()
    written_at = models.DateTimeField(default=timezone.now)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["class_term", "band_min"], name="uq_gradebook_principal_comment_band",
            ),
        ]

    def __str__(self):
        return f"{self.class_term_id}: {self.band_min}+"


class ResultHold(_Owned):
    """A student's result kept from release for a term, placed and lifted by hand.

    One open hold per student per term, which the database holds as a partial
    unique constraint over rows not yet lifted.
    """

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="gradebook_holds",
    )
    term = models.ForeignKey(
        "vs_academics.AcademicTerm", on_delete=models.PROTECT,
        related_name="gradebook_holds",
    )
    student = models.ForeignKey(
        "vs_students.Student", on_delete=models.PROTECT, related_name="gradebook_holds",
    )
    reason = models.TextField()
    placed_by = _user_fk()
    placed_at = models.DateTimeField(default=timezone.now)
    lifted_by = _user_fk()
    lifted_at = models.DateTimeField(null=True, blank=True)
    lift_note = models.TextField(blank=True, default="")

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["term", "student"], condition=Q(lifted_at__isnull=True),
                name="uq_gradebook_hold_open_term_student",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "term"])]

    def __str__(self):
        return f"{self.student_id} / {self.term_id}"

    @property
    def is_open(self) -> bool:
        return self.lifted_at is None


class SheetReminder(_Owned):
    """A reminder sent to a sheet's main teacher on one school day."""

    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT,
        related_name="gradebook_reminders",
    )
    sheet = models.ForeignKey(
        ScoreSheet, on_delete=models.CASCADE, related_name="reminders",
    )
    on_date = models.DateField()
    sent_by = _user_fk()
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta(_Owned.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["sheet", "on_date"], name="uq_gradebook_reminder_sheet_date",
            ),
        ]
        indexes = [models.Index(fields=["tenant", "on_date"])]
