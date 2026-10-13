"""The class teacher's part: a comment and ratings for each student, then the class submitted.

**Compiling a student** saves the class teacher's comment, which is required,
and the 1 to 5 ratings of the school's traits where the school records them.
Ratings are replaced as a whole when the request carries them and left alone
when it does not, so a form that only edits the comment cannot clear them. The
class term and the student's result row are created by the first save. A save is
refused once the class has gone to the principal or been published, because the
comment is then part of what the principal is asked to approve
(:class:`~..exceptions.ClassLocked`).

**Submitting the class** is the class teacher's one-way step to the principal.
It needs every due sheet in (``SUBJECTS_MISSING``, naming the subjects) and a
comment for every student on the roll (``COMMENTS_MISSING``, with the count). It
sets the stage to AWAITING with who and when, and clears the note the class was
last returned with, because the correction it asked for has been made. The
checks and the change are made under a lock on the class's term, so a sheet
returned a moment earlier is seen and a second submit cannot race the first.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from vs_audit.models import AuditActionType

from ..constants import (
    RATING_MAX,
    RATING_MIN,
    StoredStage,
)
from ..exceptions import (
    ClassLocked,
    CommentRequired,
    CommentsMissing,
    InvalidRating,
    SubjectsMissing,
)
from ..models import ClassTerm, StudentTermResult
from . import audit, book, classbook, resolve
from .classbook import BOOK_RULES, ClassContext
from .rules import read_gradebook_rules
from .sheets import LOCKED_STAGES


def _label(ctx: ClassContext) -> str:
    return f"{ctx.school_class.name}, {ctx.term.name}"


def clean_ratings(rules, ratings):
    """*ratings* as a mapping keyed by the school's traits, or raise :class:`InvalidRating`.

    None (the request carried none) stays None. An empty mapping clears the
    student's ratings. Every key must be one of the school's traits and every
    value a whole number from 1 to 5; a school that does not record conduct
    ratings accepts none.
    """
    if ratings is None:
        return None
    if not isinstance(ratings, dict):
        raise InvalidRating("Send the ratings as a trait and a number for each.", field="ratings")
    if not ratings:
        return {}
    if not rules.conduct_ratings:
        raise InvalidRating(
            "This school does not record conduct ratings, so there is nothing to rate.",
            field="ratings",
        )
    unknown = sorted(name for name in ratings if name not in rules.rating_traits)
    if unknown:
        raise InvalidRating(
            f"{', '.join(unknown)} {'is not a trait' if len(unknown) == 1 else 'are not traits'} "
            f"this school rates. Rate only the traits listed.",
            field="ratings", traits=unknown,
        )
    bad = sorted(
        name for name, value in ratings.items()
        if isinstance(value, bool) or not isinstance(value, int)
        or not RATING_MIN <= value <= RATING_MAX
    )
    if bad:
        raise InvalidRating(
            f"Rate {', '.join(bad)} with a whole number from {RATING_MIN} to {RATING_MAX}.",
            field="ratings", traits=bad,
        )
    return {name: ratings[name] for name in rules.rating_traits if name in ratings}


def _student_on_roll(ctx: ClassContext, student_id):
    roll = book.rolls_for(ctx.tenant, ctx.term, [(ctx.school_class, ctx.branch)])
    for entry in roll.get(ctx.school_class.pk, []):
        if entry.student.pk == student_id:
            return entry.student
    raise NotFound("No such student on this class's roll.")


def _lock(ctx: ClassContext) -> ClassTerm:
    """The class's term, created if need be, re-read under a row lock and refused if it is locked."""
    class_term = resolve.class_term(ctx.tenant, ctx.school_class, ctx.term, create=True)
    return ClassTerm.objects.select_for_update().get(pk=class_term.pk)


def save_compile(ctx: ClassContext, student_id, comment, ratings) -> None:
    """Save the class teacher's comment, and the ratings when *ratings* is not None.

    The caller has already judged who may write. Refused with
    :class:`~..exceptions.CommentRequired` for a blank comment and
    :class:`~..exceptions.ClassLocked` once the class has gone to the principal.
    """
    comment = (comment or "").strip()
    if not comment:
        raise CommentRequired(
            "Write a comment for this student before saving.", field="teacher_comment",
        )
    rules = read_gradebook_rules(ctx.tenant, keys=BOOK_RULES)
    cleaned = clean_ratings(rules, ratings)
    student = _student_on_roll(ctx, student_id)
    existing = resolve.class_term(ctx.tenant, ctx.school_class, ctx.term)
    if existing is not None and existing.stage in LOCKED_STAGES:
        raise ClassLocked()

    now = timezone.now()
    with transaction.atomic():
        class_term = _lock(ctx)
        if class_term.stage in LOCKED_STAGES:
            raise ClassLocked()
        result, _ = StudentTermResult.objects.get_or_create(
            tenant=ctx.tenant, class_term=class_term, student=student,
        )
        result.teacher_comment = comment
        result.comment_by, result.comment_at = ctx.user, now
        fields = ["teacher_comment", "comment_by", "comment_at", "updated_at"]
        if cleaned is not None:
            result.ratings = cleaned
            fields.append("ratings")
        result.save(update_fields=fields)
        audit.record(
            tenant=ctx.tenant, user=ctx.user, branch=ctx.branch,
            entity_type="StudentTermResult", entity_id=result.pk,
            entity_label=f"{student.full_name}, {_label(ctx)}",
            summary=(
                f"{ctx.user.full_name} wrote the class teacher's comment for "
                f"{student.full_name}, {_label(ctx)}."
            ),
            class_id=ctx.school_class.pk, student_id=student.pk, term_id=ctx.term.pk,
            ratings_saved=cleaned is not None,
        )


def _names(columns) -> str:
    names = [column.subject.name for column in columns]
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def submit_class(ctx: ClassContext) -> None:
    """Send the class to the principal, or refuse and say what is still missing."""
    if (existing := resolve.class_term(ctx.tenant, ctx.school_class, ctx.term)) is not None \
            and existing.stage in LOCKED_STAGES:
        raise ClassLocked(
            "This class has already been submitted to the principal."
            if existing.stage == StoredStage.AWAITING
            else "This class's results are already published.",
        )
    now = timezone.now()
    with transaction.atomic():
        class_term = _lock(ctx)
        if class_term.stage in LOCKED_STAGES:
            raise ClassLocked("This class has already been submitted to the principal.")
        bk = classbook.load_book(ctx.tenant, ctx.school_class, ctx.branch, ctx.term)
        if not bk.columns:
            raise SubjectsMissing(
                "This class has no subject with students on it yet, so there is "
                "nothing to submit.",
                subjects=[],
            )
        missing = bk.missing
        if missing:
            raise SubjectsMissing(
                f"{_names(missing)} {'is' if len(missing) == 1 else 'are'} not in yet. "
                f"Every subject's sheet must be in before the class can be submitted.",
                subjects=[
                    {"id": column.subject.pk, "name": column.subject.name} for column in missing
                ],
            )
        absent = bk.comments_missing
        if absent:
            raise CommentsMissing(
                f"{absent} {'student has' if absent == 1 else 'students have'} no comment "
                f"yet. Write one for every student before submitting the class.",
                count=absent,
            )
        class_term.stage = StoredStage.AWAITING
        class_term.submitted_by, class_term.submitted_at = ctx.user, now
        class_term.returned_by, class_term.returned_at, class_term.returned_note = None, None, ""
        class_term.save(update_fields=[
            "stage", "submitted_by", "submitted_at", "returned_by", "returned_at",
            "returned_note", "updated_at",
        ])
        audit.record(
            tenant=ctx.tenant, user=ctx.user, branch=ctx.branch,
            entity_type="ClassTerm", entity_id=class_term.pk, entity_label=_label(ctx),
            summary=f"{ctx.user.full_name} submitted {_label(ctx)} to the principal.",
            class_id=ctx.school_class.pk, term_id=ctx.term.pk,
            subjects=len(bk.columns), students=len(bk.roll),
            action_type=AuditActionType.UPDATE,
        )
