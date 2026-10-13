"""No broadsheet, compile, principal-comment or hold field reaches a caller who may read none.

The surfaces of the class-to-card screens, rendered as a caller whose role has
every field of the resource switched off, and the whole payload walked at every
depth. Each is rendered on the payload its service builds for a real class: the
broadsheet and the compile drawer of JSS1 A with its sheets in, the principal's
comments with one written, and the holds list with a held student, a balance
from a fake finance reader and a lifted hold. The bodies the screens send are
rendered as the dictionaries the screens would post.

Both shapes of school are covered where the shape changes a payload: Sunrise
(one branch, no ranking, no ratings) renders the broadsheet and the drawer
without the keys its settings leave out.
"""
from __future__ import annotations

import datetime as dt

from schools.core.fal import registry
from schools.core.fal.contracts import AgeingBucket, DebtorRow
from schools.core.fal.testing import FakeFinanceReader
from schools.vs_gradebook.models import ClassTerm, PrincipalComment, ResultHold
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample

from ..services import approvals, classbook, holds
from .class_base import ClassFixture

_SURFACE = "schools.vs_gradebook.serializers."


class ClassDeepPayloadTests(DeepPayloadChecks, ClassFixture):
    resources = frozenset({"gradebook.sheet", "gradebook.result", "gradebook.hold"})
    covers = frozenset({
        _SURFACE + name for name in (
            "BroadsheetCellSerializer", "CompileSubjectSerializer", "ClassReturnBodySerializer",
            "BroadsheetRowSerializer", "CompileSerializer", "CompileWriteSerializer",
            "PrincipalBandSerializer", "PrincipalCommentsWriteSerializer",
            "HoldRowSerializer", "HoldRecordSerializer", "HoldLiftedSerializer",
            "HoldPlaceBodySerializer", "HoldLiftBodySerializer",
        )
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sheets_in()
        cls.comment_all()
        ResultHold.objects.create(
            tenant=cls.tenant, term=cls.first_term, student=cls.tunde,
            reason="Fees outstanding since September.", placed_by=cls.bursar,
        )
        lifted = ResultHold.objects.create(
            tenant=cls.tenant, term=cls.first_term, student=cls.kelechi,
            reason="Arrears.", placed_by=cls.bursar, lifted_by=cls.bursar,
            lifted_at=dt.datetime(2025, 10, 12, 9, 0, tzinfo=dt.timezone.utc),
            lift_note="Paid in full.",
        )
        cls.lifted = lifted
        PrincipalComment.objects.create(
            tenant=cls.tenant, class_term=ClassTerm.objects.get(school_class=cls.jss1a),
            band_min=70, text="Outstanding.", written_by=cls.principal,
        )

    def tearDown(self):
        registry.reset()

    def samples(self):
        registry.set_finance_reader(FakeFinanceReader(debtors=(DebtorRow(
            student_ref=str(self.tunde.pk), student_name="Tunde Bello", class_label="",
            outstanding=8_500_000, ageing=AgeingBucket.DAYS_31_60,
            oldest_unpaid_on=dt.date(2025, 9, 20),
        ),)))
        bk = classbook.load_book(self.tenant, self.jss1a, self.lekki, self.first_term)
        broadsheet = classbook.broadsheet_payload(bk, self.eze)
        drawer = classbook.compile_payload(bk, self.tunde.pk, self.eze)
        ctx = classbook.ClassContext(
            self.tenant, self.principal, self.jss1a, self.lekki, self.first_term,
        )
        bands = approvals.principal_payload(ctx)["bands"]
        seats = holds.seats(self.tenant, self.bursar, self.first_term, None)
        page = holds.rows_payload(self.tenant, self.first_term, seats)
        held_row = next(r for r in page["rows"] if r["hold"] is not None)
        lifted = holds.hold_payload(self.lifted)
        return [
            Sample(_SURFACE + "BroadsheetCellSerializer", broadsheet["rows"][0]["cells"][0]),
            Sample(_SURFACE + "BroadsheetRowSerializer", broadsheet["rows"][0]),
            Sample(_SURFACE + "CompileSubjectSerializer", drawer["subjects"][0]),
            Sample(_SURFACE + "CompileSerializer", drawer),
            Sample(_SURFACE + "CompileWriteSerializer", {
                "teacher_comment": "A joy to teach.", "ratings": {"Punctuality": 5},
            }),
            Sample(_SURFACE + "ClassReturnBodySerializer", {"note": "Recheck.", "subject": 3}),
            Sample(_SURFACE + "PrincipalBandSerializer", bands[0]),
            Sample(_SURFACE + "PrincipalCommentsWriteSerializer", {
                "comments": [{"band_min": 70, "text": "Outstanding."}],
            }),
            Sample(_SURFACE + "HoldRowSerializer", held_row),
            Sample(_SURFACE + "HoldRecordSerializer", held_row["hold"]),
            Sample(_SURFACE + "HoldLiftedSerializer", lifted),
            Sample(_SURFACE + "HoldPlaceBodySerializer", {
                "student": self.tunde.pk, "term": self.first_term.pk, "reason": "Fees.",
            }),
            Sample(_SURFACE + "HoldLiftBodySerializer", {"lift_note": "Paid in full."}),
        ]
