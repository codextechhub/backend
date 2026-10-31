"""Results sections of a personal data copy (:mod:`core.personal_data`)."""
from __future__ import annotations

from core.personal_data import PUPIL, register, rows


def _of_pupil(model_name):
    def build(pupil):
        from django.apps import apps

        model = apps.get_model("vs_gradebook", model_name)
        return rows(model._base_manager.filter(student=pupil).order_by("pk"), skip=("student",))

    return build


def register_sections() -> None:
    register(PUPIL, "term_results", "Term results", _of_pupil("StudentTermResult"),
             covers=("vs_gradebook.StudentTermResult",))
    register(PUPIL, "scores", "Scores", _of_pupil("Score"), covers=("vs_gradebook.Score",))
    register(PUPIL, "score_amendments", "Score corrections", _of_pupil("Amendment"),
             covers=("vs_gradebook.Amendment",))
    register(PUPIL, "result_holds", "Results held back", _of_pupil("ResultHold"),
             covers=("vs_gradebook.ResultHold",))
