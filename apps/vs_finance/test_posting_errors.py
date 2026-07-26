from django.test import SimpleTestCase

from vs_finance.exceptions import PeriodClosedError
from vs_finance.posting import ensure_period_open


class PostingErrorMessageTests(SimpleTestCase):
    def test_missing_period_has_concise_user_facing_message(self):
        with self.assertRaisesMessage(
            PeriodClosedError,
            (
                "This date is outside your fiscal periods. "
                "Choose a date within an open fiscal period."
            ),
        ):
            ensure_period_open(None)
