from django.test import SimpleTestCase

from vs_finance.exceptions import PeriodClosedError
from vs_finance.posting import ensure_period_open


class PostingErrorMessageTests(SimpleTestCase):
    def test_missing_period_has_concise_user_facing_message(self):
        # The usual cause of "no period covers this date" is that nobody created the
        # next fiscal year, and then no date works - so the message names that fix
        # rather than telling the operator to pick a different date.
        with self.assertRaisesMessage(
            PeriodClosedError,
            (
                "No fiscal period covers this date. Either the date falls outside "
                "your fiscal calendar, or the next fiscal year has not been created "
                "yet. Open Fiscal Periods and create the next fiscal year if the "
                "calendar has run out."
            ),
        ):
            ensure_period_open(None)
