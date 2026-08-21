"""Central or per-branch payroll, and the one rule that spans both.

A school runs payroll one of two ways and says which through ``payroll.scope``:
CENTRAL, one run for everybody, which is what every school does and the default
nobody has to choose; or PER_BRANCH, where each site's payroll officer raises a run
covering exactly her own staff. Both shapes are first class and the school picks.

The rule underneath is that **a person is paid exactly once per period**, and
almost every test here exists to defend it from a different direction:

* a branch run reads the roster **exclusively** - that branch's rows and nothing
  else. Everywhere else in this codebase a null branch reads inclusively and means
  "shared across the school"; here it would mean an unassigned person appearing on
  Ikeja's run, Lekki's run *and* Yaba's run, and being paid three times;
* nothing may be unassigned by the time that matters, so switching a school to
  PER_BRANCH is refused while any active employee has no branch, by name;
* two runs whose coverage overlaps cannot exist in one period.

The other half of the file is the promise the whole design rests on: a CENTRAL
school - which is every school, today - must be unable to tell that any of this was
built. Those tests are deliberately dull and deliberately first.

Two shapes of school throughout, because a single-branch test proves nothing about
a multi-branch one.
"""
from __future__ import annotations

import datetime

from vs_finance.models import EmployeeSalary, PayrollRun

from .tests_branch_scope import _FinanceBranchFixture


class _PayrollFixture(_FinanceBranchFixture):
    """The branch fixture plus a roster, payroll grants and the scope setting."""

    PAYROLL_KEYS = (
        "finance.payrollrun.create", "finance.payrollrun.view",
        "finance.payrollrun.view_sensitive",
        "finance.salary.create", "finance.salary.view", "finance.salary.update",
        "finance.salary.delete",
    )

    # -- people ---------------------------------------------------------------- #

    def officer(self, tenant, email, role_key, *, branches=()):
        """A payroll officer pinned to zero, one or several branches.

        Several branches means several grants: an assignment carries one branch, so
        "covers Ikeja and Lekki" is two rows rather than one, and that is the shape
        the ambiguous case actually arrives in.
        """
        from core.test_utils import TenantAPIClient

        user = self.user_for(tenant, email)
        if not branches:
            self.grant(user, *self.PAYROLL_KEYS, tenant=tenant, role_key=role_key)
        else:
            for index, branch in enumerate(branches):
                self.grant(
                    user, *self.PAYROLL_KEYS, tenant=tenant,
                    role_key=f"{role_key}-{index}", branch=branch,
                )
        return TenantAPIClient(user=user)

    # -- roster ---------------------------------------------------------------- #

    def salary(self, entity, name, branch=None, *, gross=50_000_00, active=True):
        return EmployeeSalary.objects.create(
            entity=entity, name=name, branch=branch,
            gross_amount=gross, is_active=active,
        )

    # -- the setting ----------------------------------------------------------- #

    def set_scope(self, tenant, value, *, actor=None):
        """Switch a school's payroll scope through the real configuration write.

        Through ``set_value`` rather than by writing the row, so every switch in
        this file passes the guard a bursar would meet, and a test that needs a
        school already on PER_BRANCH has to assign its roster first - which is
        exactly the sequence the product requires.
        """
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        definition = ConfigurationDefinition.objects.get(key="payroll.scope")
        return set_value(
            definition=definition, value=value, actor=actor, tenant=tenant,
            reason="test",
        )

    # -- calling --------------------------------------------------------------- #

    def generate(self, client, entity, **body):
        payload = {"pay_date": "2026-01-25", "period_label": "January 2026", **body}
        return client.post(
            f"/v1/finance/payroll-runs/generate/?entity={entity.code}",
            payload, format="json",
        )

    def create_run(self, client, entity, **body):
        payload = {
            "pay_date": "2026-01-25", "period_label": "January 2026",
            "lines": [{"employee_name": "A Teacher", "gross_amount": 500_00}],
            **body,
        }
        return client.post(
            f"/v1/finance/payroll-runs/?entity={entity.code}", payload, format="json",
        )

    def names_on(self, run):
        return sorted(line.employee_name for line in run.lines.all())


# --------------------------------------------------------------------------- #
# The promise: a central school cannot tell any of this was built              #
# --------------------------------------------------------------------------- #


class CentralPayrollIsUnchangedTests(_PayrollFixture):
    """CENTRAL is the default and it must behave exactly as it did before.

    This is the assumption the whole feature is shipped on: per-branch payroll is
    opt-in, so no school that has not opted in may see a different roster, a
    different run, a new refusal or a new required field. Every test here would
    have passed against the code as it stood before the branch column existed.
    """

    def setUp(self):
        super().setUp()
        self.salary(self.books, "Ada Obi")
        self.salary(self.books, "Bola Lawal")
        self.salary(self.books, "Chidi Eze")

    def test_no_school_has_opted_in_by_default(self):
        """The setting exists, and it says CENTRAL until somebody says otherwise."""
        from vs_finance.payroll import payroll_scope

        self.assertEqual(payroll_scope(self.books), "CENTRAL")
        self.assertEqual(payroll_scope(self.solo_books), "CENTRAL")

    def test_a_generated_run_still_covers_the_whole_roster(self):
        hq = self.officer(self.tenant, "central-hq@fin.test", "c-hq")

        response = self.generate(hq, self.books)

        self.assertEqual(response.status_code, 201, response.data)
        run = PayrollRun.objects.get(entity=self.books)
        self.assertEqual(self.names_on(run), ["Ada Obi", "Bola Lawal", "Chidi Eze"])
        self.assertIsNone(run.branch_id)

    def test_a_branch_pinned_officer_still_runs_the_whole_school(self):
        """The case that would have broken, and the reason the setting exists.

        Mrs Bello is granted Bursar at Ikeja because that is where she sits, not
        because Corona runs payroll per site - Corona runs one payroll. Her roster
        carries no branches at all. If her pinned grant were allowed to narrow the
        run she raises, it would select on a column nobody has filled in, match
        nothing, and her January payroll would come back "no active employees" for
        a school with three of them.
        """
        bello = self.officer(
            self.tenant, "central-ikeja@fin.test", "c-ikeja", branches=[self.ikeja],
        )

        response = self.generate(bello, self.books)

        self.assertEqual(response.status_code, 201, response.data)
        run = PayrollRun.objects.get(entity=self.books)
        self.assertEqual(self.names_on(run), ["Ada Obi", "Bola Lawal", "Chidi Eze"])
        self.assertIsNone(run.branch_id)

    def test_an_officer_covering_two_branches_is_not_asked_to_pick(self):
        """Under CENTRAL there is nothing to pick between: the run covers everyone."""
        both = self.officer(
            self.tenant, "central-both@fin.test", "c-both",
            branches=[self.ikeja, self.lekki],
        )

        response = self.generate(both, self.books)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(PayrollRun.objects.get(entity=self.books).lines.count(), 3)

    def test_a_second_run_in_the_same_month_is_still_allowed(self):
        """Advances and supplementary payments have always been possible here.

        The overlap guard is a PER_BRANCH rule. Turning it on for a central school
        would be this change quietly taking away something schools use, on the way
        to fixing something they had not asked about.
        """
        hq = self.officer(self.tenant, "central-twice@fin.test", "c-twice")

        first = self.generate(hq, self.books)
        second = self.generate(hq, self.books, pay_date="2026-01-31")

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(PayrollRun.objects.filter(entity=self.books).count(), 2)

    def test_the_roster_screen_still_shows_everybody_to_a_pinned_officer(self):
        bello = self.officer(
            self.tenant, "central-roster@fin.test", "c-roster", branches=[self.ikeja],
        )

        response = bello.get(
            f"/v1/finance/employee-salaries/?entity={self.books.code}",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["name"] for row in response.data["data"]],
            ["Ada Obi", "Bola Lawal", "Chidi Eze"],
        )

    def test_adding_an_employee_still_needs_no_branch(self):
        hq = self.officer(self.tenant, "central-add@fin.test", "c-add")

        response = hq.post(
            f"/v1/finance/employee-salaries/?entity={self.books.code}",
            {"name": "Dele Ade", "gross_amount": 40_000_00}, format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(
            EmployeeSalary.objects.get(entity=self.books, name="Dele Ade").branch_id,
        )

    def test_a_single_branch_school_runs_centrally_too(self):
        """One branch is the common case and must not become a special case."""
        self.salary(self.solo_books, "Solo Staff")
        solo = self.officer(
            self.solo_tenant, "central-solo@fin.test", "c-solo",
            branches=[self.solo_main],
        )

        response = self.generate(solo, self.solo_books)

        self.assertEqual(response.status_code, 201, response.data)
        run = PayrollRun.objects.get(entity=self.solo_books)
        self.assertEqual(self.names_on(run), ["Solo Staff"])
        self.assertIsNone(run.branch_id)


# --------------------------------------------------------------------------- #
# Switching over                                                              #
# --------------------------------------------------------------------------- #


class SwitchingToPerBranchTests(_PayrollFixture):
    """The one moment somebody is paying attention, so the guard lives here."""

    def test_the_switch_is_refused_while_anyone_is_unassigned_and_names_them(self):
        """The gap this design closes, closed at the only place it can be.

        Corona assigns its teachers but forgets the principal and the group
        accountant. Let the switch through and the three branch runs pay everybody
        else, nobody's run reaches those two, and the first anyone hears of it is
        two people asking where January's salary went. Refusing here costs the
        bursar two edits.
        """
        from vs_config.exceptions import InvalidConfigurationValue

        self.salary(self.books, "Ada Obi", self.ikeja)
        self.salary(self.books, "Mrs Okonjo")
        self.salary(self.books, "Group Accountant")

        with self.assertRaises(InvalidConfigurationValue) as caught:
            self.set_scope(self.tenant, "PER_BRANCH")

        self.assertIn("Mrs Okonjo", str(caught.exception))
        self.assertIn("Group Accountant", str(caught.exception))
        from vs_finance.payroll import payroll_scope
        self.assertEqual(payroll_scope(self.books), "CENTRAL")

    def test_an_inactive_employee_does_not_block_the_switch(self):
        """A leaver is not somebody waiting to be paid."""
        self.salary(self.books, "Ada Obi", self.ikeja)
        self.salary(self.books, "Retired Teacher", None, active=False)

        self.set_scope(self.tenant, "PER_BRANCH")

        from vs_finance.payroll import payroll_scope
        self.assertEqual(payroll_scope(self.books), "PER_BRANCH")

    def test_the_switch_is_allowed_once_everybody_has_a_branch(self):
        self.salary(self.books, "Ada Obi", self.ikeja)
        self.salary(self.books, "Bola Lawal", self.lekki)
        self.salary(self.books, "Chidi Eze", self.yaba)

        self.set_scope(self.tenant, "PER_BRANCH")

        from vs_finance.payroll import payroll_scope
        self.assertEqual(payroll_scope(self.books), "PER_BRANCH")

    def test_the_check_covers_the_whole_school_not_one_set_of_books(self):
        """A group keeping two sets of books cannot switch on the strength of one."""
        from vs_config.exceptions import InvalidConfigurationValue

        other_books = self.build_books("FINMULTI2", self.tenant)
        self.salary(self.books, "Ada Obi", self.ikeja)
        self.salary(other_books, "Forgotten Cook")

        with self.assertRaises(InvalidConfigurationValue) as caught:
            self.set_scope(self.tenant, "PER_BRANCH")

        self.assertIn("Forgotten Cook", str(caught.exception))

    def test_one_schools_unassigned_roster_does_not_block_another(self):
        self.salary(self.books, "Unassigned Person")
        self.salary(self.solo_books, "Solo Staff", self.solo_main)

        self.set_scope(self.solo_tenant, "PER_BRANCH")

        from vs_finance.payroll import payroll_scope
        self.assertEqual(payroll_scope(self.solo_books), "PER_BRANCH")
        self.assertEqual(payroll_scope(self.books), "CENTRAL")

    def test_switching_back_to_central_is_always_allowed(self):
        """Going back is safe by construction: a central run covers everybody."""
        self.salary(self.books, "Ada Obi", self.ikeja)
        self.set_scope(self.tenant, "PER_BRANCH")
        self.salary(self.books, "Newly Hired")  # unassigned, added afterwards

        self.set_scope(self.tenant, "CENTRAL")

        from vs_finance.payroll import payroll_scope
        self.assertEqual(payroll_scope(self.books), "CENTRAL")

    def test_the_roster_screen_can_list_exactly_who_is_blocking_the_switch(self):
        self.salary(self.books, "Ada Obi", self.ikeja)
        self.salary(self.books, "Mrs Okonjo")
        hq = self.officer(self.tenant, "switch-hq@fin.test", "s-hq")

        response = hq.get(
            f"/v1/finance/employee-salaries/"
            f"?entity={self.books.code}&branch=unassigned",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["name"] for row in response.data["data"]], ["Mrs Okonjo"])


# --------------------------------------------------------------------------- #
# Running per branch                                                          #
# --------------------------------------------------------------------------- #


class PerBranchRunTests(_PayrollFixture):
    """What a branch run covers once a school has switched."""

    def setUp(self):
        super().setUp()
        self.salary(self.books, "Ikeja Teacher", self.ikeja)
        self.salary(self.books, "Lekki Teacher", self.lekki)
        self.salary(self.books, "Yaba Teacher", self.yaba)
        self.set_scope(self.tenant, "PER_BRANCH")
        self.bello = self.officer(
            self.tenant, "pb-ikeja@fin.test", "pb-ikeja", branches=[self.ikeja],
        )

    def test_a_branch_run_covers_only_that_branchs_staff(self):
        response = self.generate(self.bello, self.books)

        self.assertEqual(response.status_code, 201, response.data)
        run = PayrollRun.objects.get(entity=self.books)
        self.assertEqual(self.names_on(run), ["Ikeja Teacher"])
        self.assertEqual(run.branch_id, self.ikeja.pk)

    def test_a_branch_run_does_not_reach_an_unassigned_row(self):
        """The exclusive reading, stated as the thing it prevents.

        A row can become unassigned after the switch - a new hire typed in by an
        unpinned head-office bursar. Read inclusively it would join Ikeja's run,
        Lekki's run and Yaba's run at once and be paid three times, which is why
        payroll refuses the platform default here.
        """
        self.salary(self.books, "Newly Hired")

        response = self.generate(self.bello, self.books)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            self.names_on(PayrollRun.objects.get(entity=self.books)), ["Ikeja Teacher"],
        )

    def test_an_unassigned_person_is_never_on_two_runs(self):
        """The rule itself, proved across every branch a school has."""
        self.salary(self.books, "Newly Hired")
        lekki = self.officer(
            self.tenant, "pb-lekki@fin.test", "pb-lekki", branches=[self.lekki],
        )
        yaba = self.officer(
            self.tenant, "pb-yaba@fin.test", "pb-yaba", branches=[self.yaba],
        )

        for client in (self.bello, lekki, yaba):
            self.assertEqual(self.generate(client, self.books).status_code, 201)

        appearances = [
            run.branch_id for run in PayrollRun.objects.filter(entity=self.books)
            if "Newly Hired" in self.names_on(run)
        ]
        self.assertEqual(appearances, [])

    def test_each_branch_pays_its_own_people_exactly_once(self):
        lekki = self.officer(
            self.tenant, "pb-lekki2@fin.test", "pb-lekki2", branches=[self.lekki],
        )
        yaba = self.officer(
            self.tenant, "pb-yaba2@fin.test", "pb-yaba2", branches=[self.yaba],
        )

        for client in (self.bello, lekki, yaba):
            self.assertEqual(self.generate(client, self.books).status_code, 201)

        paid = [
            name for run in PayrollRun.objects.filter(entity=self.books)
            for name in self.names_on(run)
        ]
        self.assertEqual(
            sorted(paid), ["Ikeja Teacher", "Lekki Teacher", "Yaba Teacher"],
        )
        self.assertEqual(len(paid), len(set(paid)))

    def test_an_empty_branch_roster_says_which_branch_is_empty(self):
        """Yaba's officer must not be told the school has no employees."""
        EmployeeSalary.objects.filter(entity=self.books, branch=self.yaba).delete()
        yaba = self.officer(
            self.tenant, "pb-empty@fin.test", "pb-empty", branches=[self.yaba],
        )

        response = self.generate(yaba, self.books)

        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("Yaba Branch", str(response.data))

    def test_a_single_branch_school_runs_its_one_branch(self):
        self.salary(self.solo_books, "Solo Staff", self.solo_main)
        self.set_scope(self.solo_tenant, "PER_BRANCH")
        solo = self.officer(
            self.solo_tenant, "pb-solo@fin.test", "pb-solo", branches=[self.solo_main],
        )

        response = self.generate(solo, self.solo_books)

        self.assertEqual(response.status_code, 201, response.data)
        run = PayrollRun.objects.get(entity=self.solo_books)
        self.assertEqual(self.names_on(run), ["Solo Staff"])
        self.assertEqual(run.branch_id, self.solo_main.pk)


class PerBranchStampingTests(_PayrollFixture):
    """Which branch a run is stamped with, and who may name one."""

    def setUp(self):
        super().setUp()
        self.salary(self.books, "Ikeja Teacher", self.ikeja)
        self.salary(self.books, "Lekki Teacher", self.lekki)
        self.salary(self.books, "Yaba Teacher", self.yaba)
        self.set_scope(self.tenant, "PER_BRANCH")

    def test_a_pinned_officer_stamps_her_own_branch_without_asking(self):
        bello = self.officer(
            self.tenant, "st-ikeja@fin.test", "st-ikeja", branches=[self.ikeja],
        )

        response = self.generate(bello, self.books)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            PayrollRun.objects.get(entity=self.books).branch_id, self.ikeja.pk,
        )

    def test_an_unpinned_officer_running_centrally_stamps_nothing(self):
        """Head office may still run the whole school in one go, and does.

        Under PER_BRANCH that covers everybody exactly once, because everybody has
        a branch; the overlap guard is what stops it happening alongside the branch
        runs.
        """
        hq = self.officer(self.tenant, "st-hq@fin.test", "st-hq")

        response = self.generate(hq, self.books)

        self.assertEqual(response.status_code, 201, response.data)
        run = PayrollRun.objects.get(entity=self.books)
        self.assertIsNone(run.branch_id)
        self.assertEqual(
            self.names_on(run), ["Ikeja Teacher", "Lekki Teacher", "Yaba Teacher"],
        )

    def test_a_pinned_officer_naming_another_branch_is_refused(self):
        """Refused rather than retargeted: silently rewriting it hides the mistake."""
        bello = self.officer(
            self.tenant, "st-ikeja2@fin.test", "st-ikeja2", branches=[self.ikeja],
        )

        response = self.generate(bello, self.books, branch=self.lekki.pk)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(PayrollRun.objects.filter(entity=self.books).exists())

    def test_an_officer_covering_two_branches_is_asked_which_one(self):
        """The two runs pay different people, and only she knows which she means."""
        both = self.officer(
            self.tenant, "st-both@fin.test", "st-both",
            branches=[self.ikeja, self.lekki],
        )

        response = self.generate(both, self.books)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("branch", response.data["error"]["detail"])
        self.assertFalse(PayrollRun.objects.filter(entity=self.books).exists())

    def test_an_officer_covering_two_branches_may_name_either_of_hers(self):
        both = self.officer(
            self.tenant, "st-both2@fin.test", "st-both2",
            branches=[self.ikeja, self.lekki],
        )

        response = self.generate(both, self.books, branch=self.lekki.pk)

        self.assertEqual(response.status_code, 201, response.data)
        run = PayrollRun.objects.get(entity=self.books)
        self.assertEqual(run.branch_id, self.lekki.pk)
        self.assertEqual(self.names_on(run), ["Lekki Teacher"])

    def test_another_tenants_branch_is_refused_like_an_unknown_one(self):
        """The field must not become an oracle for ids outside the caller's school."""
        hq = self.officer(self.tenant, "st-hq2@fin.test", "st-hq2")

        foreign = self.generate(hq, self.books, branch=self.rival_branch.pk)
        unknown = self.generate(hq, self.books, branch=99_999_999)

        self.assertEqual(foreign.status_code, 400, foreign.data)
        self.assertEqual(unknown.status_code, 400, unknown.data)
        self.assertEqual(
            foreign.data["error"]["detail"], unknown.data["error"]["detail"],
        )


# --------------------------------------------------------------------------- #
# The overlap guard                                                           #
# --------------------------------------------------------------------------- #


class OverlappingRunTests(_PayrollFixture):
    """Two runs covering the same person in one period is a double payment."""

    def setUp(self):
        super().setUp()
        self.salary(self.books, "Ikeja Teacher", self.ikeja)
        self.salary(self.books, "Lekki Teacher", self.lekki)
        self.salary(self.books, "Yaba Teacher", self.yaba)
        self.set_scope(self.tenant, "PER_BRANCH")
        self.bello = self.officer(
            self.tenant, "ov-ikeja@fin.test", "ov-ikeja", branches=[self.ikeja],
        )
        self.hq = self.officer(self.tenant, "ov-hq@fin.test", "ov-hq")

    def test_a_central_run_is_refused_once_a_branch_run_exists(self):
        """The real double-payment attempt, in the order it would happen.

        Ikeja runs its own January payroll on the 25th. On the 31st head office,
        not knowing, runs the whole school. Without this, Ikeja's teachers are
        accrued twice and the bank sends their salary twice.
        """
        self.assertEqual(self.generate(self.bello, self.books).status_code, 201)

        second = self.generate(self.hq, self.books, pay_date="2026-01-31")

        self.assertEqual(second.status_code, 422, second.data)
        self.assertIn("Ikeja Branch", str(second.data))
        self.assertEqual(PayrollRun.objects.filter(entity=self.books).count(), 1)

    def test_a_branch_run_is_refused_once_a_central_run_exists(self):
        """The same collision, discovered from the other side."""
        self.assertEqual(self.generate(self.hq, self.books).status_code, 201)

        second = self.generate(self.bello, self.books, pay_date="2026-01-31")

        self.assertEqual(second.status_code, 422, second.data)
        self.assertEqual(PayrollRun.objects.filter(entity=self.books).count(), 1)

    def test_the_same_branch_twice_in_one_period_is_refused(self):
        self.assertEqual(self.generate(self.bello, self.books).status_code, 201)

        second = self.generate(self.bello, self.books, pay_date="2026-01-31")

        self.assertEqual(second.status_code, 422, second.data)
        self.assertEqual(PayrollRun.objects.filter(entity=self.books).count(), 1)

    def test_different_branches_in_one_period_are_the_whole_point(self):
        lekki = self.officer(
            self.tenant, "ov-lekki@fin.test", "ov-lekki", branches=[self.lekki],
        )

        self.assertEqual(self.generate(self.bello, self.books).status_code, 201)
        second = self.generate(lekki, self.books)

        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(PayrollRun.objects.filter(entity=self.books).count(), 2)

    def test_a_different_period_is_not_an_overlap(self):
        self.assertEqual(self.generate(self.bello, self.books).status_code, 201)

        february = self.generate(
            self.bello, self.books, pay_date="2026-02-25", period_label="February 2026",
        )

        self.assertEqual(february.status_code, 201, february.data)

    def test_a_relabelled_run_in_the_same_month_still_collides(self):
        """``period_label`` is free text and cannot be the key.

        "Jan 2026" and "January 2026" are the same payroll to everybody except a
        string comparison, and a guard those two walk past is a guard that lets
        somebody be paid twice.
        """
        self.assertEqual(
            self.generate(self.bello, self.books, period_label="January 2026").status_code,
            201,
        )

        again = self.generate(
            self.bello, self.books, pay_date="2026-01-28", period_label="Jan 2026",
        )

        self.assertEqual(again.status_code, 422, again.data)

    def test_a_voided_run_stops_blocking_the_replacement(self):
        """Voiding is how a school corrects the run it raised in error."""
        from vs_finance.payroll import cancel_payroll_run

        self.assertEqual(self.generate(self.bello, self.books).status_code, 201)
        cancel_payroll_run(PayrollRun.objects.get(entity=self.books))

        again = self.generate(self.bello, self.books)

        self.assertEqual(again.status_code, 201, again.data)

    def test_the_hand_typed_run_is_guarded_at_the_same_rule(self):
        """The other door into the same table.

        Typing the lines rather than drawing them from the roster does not make a
        second run for the period any less of a double payment, so both creation
        paths meet the same guard.
        """
        self.assertEqual(self.generate(self.bello, self.books).status_code, 201)

        typed = self.create_run(self.hq, self.books, pay_date="2026-01-31")

        self.assertEqual(typed.status_code, 422, typed.data)


# --------------------------------------------------------------------------- #
# The roster itself                                                           #
# --------------------------------------------------------------------------- #


class RosterScopingTests(_PayrollFixture):
    """Who may read and who may rewrite a salary row."""

    def setUp(self):
        super().setUp()
        self.ikeja_row = self.salary(self.books, "Ikeja Teacher", self.ikeja)
        self.lekki_row = self.salary(self.books, "Lekki Teacher", self.lekki)
        self.loose_row = self.salary(self.books, "Unassigned Person")
        self.bello = self.officer(
            self.tenant, "rs-ikeja@fin.test", "rs-ikeja", branches=[self.ikeja],
        )

    def test_a_pinned_officer_reads_her_own_rows_and_the_unassigned_ones(self):
        """Inclusive on read, exclusive on pay - somebody has to assign that row."""
        response = self.bello.get(
            f"/v1/finance/employee-salaries/?entity={self.books.code}",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["name"] for row in response.data["data"]],
            ["Ikeja Teacher", "Unassigned Person"],
        )

    def test_a_pinned_officer_cannot_rewrite_another_branchs_pay(self):
        """The write-side half of the read narrowing, and the worst hole here.

        Gross pay is the most sensitive column finance has. Without narrowing the
        lookup, Ikeja's officer reaches Lekki's teacher by guessing a primary key.
        """
        response = self.bello.patch(
            f"/v1/finance/employee-salaries/{self.lekki_row.pk}/"
            f"?entity={self.books.code}",
            {"gross_amount": 1}, format="json",
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.lekki_row.refresh_from_db()
        self.assertEqual(self.lekki_row.gross_amount, 50_000_00)

    def test_a_pinned_officer_cannot_delete_another_branchs_row(self):
        response = self.bello.delete(
            f"/v1/finance/employee-salaries/{self.lekki_row.pk}/"
            f"?entity={self.books.code}",
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.assertTrue(EmployeeSalary.objects.filter(pk=self.lekki_row.pk).exists())

    def test_a_pinned_officer_adding_an_employee_stamps_her_branch(self):
        response = self.bello.post(
            f"/v1/finance/employee-salaries/?entity={self.books.code}",
            {"name": "New Ikeja Hire", "gross_amount": 30_000_00}, format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            EmployeeSalary.objects.get(entity=self.books, name="New Ikeja Hire").branch_id,
            self.ikeja.pk,
        )

    def test_assigning_a_branch_is_how_a_school_prepares_to_switch(self):
        hq = self.officer(self.tenant, "rs-hq@fin.test", "rs-hq")

        response = hq.patch(
            f"/v1/finance/employee-salaries/{self.loose_row.pk}/"
            f"?entity={self.books.code}",
            {"branch": self.yaba.pk}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loose_row.refresh_from_db()
        self.assertEqual(self.loose_row.branch_id, self.yaba.pk)

    def test_a_pinned_officer_cannot_move_somebody_to_another_branch(self):
        response = self.bello.patch(
            f"/v1/finance/employee-salaries/{self.ikeja_row.pk}/"
            f"?entity={self.books.code}",
            {"branch": self.lekki.pk}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.ikeja_row.refresh_from_db()
        self.assertEqual(self.ikeja_row.branch_id, self.ikeja.pk)

    def test_the_roster_can_be_filtered_to_one_branch(self):
        hq = self.officer(self.tenant, "rs-hq2@fin.test", "rs-hq2")

        response = hq.get(
            f"/v1/finance/employee-salaries/"
            f"?entity={self.books.code}&branch={self.lekki.pk}",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["name"] for row in response.data["data"]], ["Lekki Teacher"])

    def test_the_roster_row_reports_its_branch(self):
        hq = self.officer(self.tenant, "rs-hq3@fin.test", "rs-hq3")

        rows = {
            row["name"]: row for row in hq.get(
                f"/v1/finance/employee-salaries/?entity={self.books.code}",
            ).data["data"]
        }

        self.assertEqual(rows["Ikeja Teacher"]["branch_name"], "Ikeja Branch")
        self.assertIsNone(rows["Unassigned Person"]["branch_name"])


# --------------------------------------------------------------------------- #
# The service, directly                                                       #
# --------------------------------------------------------------------------- #


class RosterSelectionTests(_PayrollFixture):
    """``generate_run_from_roster`` answered against its own signature.

    Over HTTP everywhere else, because that is where the rules actually bite; here
    directly, because "an existing caller passing no branch is unaffected" is a
    statement about the function rather than about a screen.
    """

    def setUp(self):
        super().setUp()
        self.salary(self.books, "Ikeja Teacher", self.ikeja)
        self.salary(self.books, "Lekki Teacher", self.lekki)
        self.salary(self.books, "Unassigned Person")

    def test_no_branch_means_the_whole_entity(self):
        from vs_finance.payroll import generate_run_from_roster

        run = generate_run_from_roster(
            self.books, pay_date=datetime.date(2026, 1, 25),
        )

        self.assertIsNone(run.branch_id)
        self.assertEqual(
            self.names_on(run),
            ["Ikeja Teacher", "Lekki Teacher", "Unassigned Person"],
        )

    def test_a_branch_means_that_branch_and_nothing_shared(self):
        from vs_finance.payroll import generate_run_from_roster

        run = generate_run_from_roster(
            self.books, pay_date=datetime.date(2026, 1, 25), branch=self.ikeja,
        )

        self.assertEqual(run.branch_id, self.ikeja.pk)
        self.assertEqual(self.names_on(run), ["Ikeja Teacher"])

    def test_roster_for_is_exclusive_on_a_branch_and_total_without_one(self):
        from vs_finance.payroll import roster_for

        self.assertEqual(
            sorted(row.name for row in roster_for(self.books)),
            ["Ikeja Teacher", "Lekki Teacher", "Unassigned Person"],
        )
        self.assertEqual(
            [row.name for row in roster_for(self.books, self.ikeja)], ["Ikeja Teacher"],
        )

    def test_an_inactive_row_is_on_no_run_either_way(self):
        from vs_finance.payroll import roster_for

        self.salary(self.books, "Retired Teacher", self.ikeja, active=False)

        self.assertEqual(
            [row.name for row in roster_for(self.books, self.ikeja)], ["Ikeja Teacher"],
        )


# --------------------------------------------------------------------------- #
# Telling the runs apart                                                      #
# --------------------------------------------------------------------------- #


class RunsCarryTheirBranchTests(_PayrollFixture):
    """Which site a run covers has to survive the trip to the screen.

    The rule was enforced from the start - a branch run pays that branch's
    roster and nobody else - but the serializer did not say which branch, so a
    per-branch school's runs list showed several rows with the same pay date,
    the same period label and nothing to tell them apart. The officer pinned to
    one site never noticed, because ``branch_q`` had already narrowed her list
    to one row. The bursar covering the school is the one who could not tell
    Ikeja's January run from Lekki's, and she is the one who pays them.
    """

    def setUp(self):
        super().setUp()
        self.salary(self.books, "Ikeja Teacher", self.ikeja)
        self.salary(self.books, "Lekki Teacher", self.lekki)
        self.salary(self.books, "Yaba Teacher", self.yaba)
        self.hq = self.officer(self.tenant, "runs-hq@fin.test", "runs-hq")

    def _runs(self, client=None, query=""):
        response = (client or self.hq).get(
            f"/v1/finance/payroll-runs/?entity={self.books.code}{query}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def _make(self, branch):
        from vs_finance.payroll import generate_run_from_roster

        return generate_run_from_roster(
            self.books, pay_date=datetime.date(2026, 1, 25), branch=branch,
        )

    def test_a_branch_run_says_which_branch(self):
        self._make(self.ikeja)

        row = self._runs()[0]

        self.assertEqual(row["branch_id"], self.ikeja.pk)
        self.assertEqual(row["branch_name"], "Ikeja Branch")

    def test_a_central_run_says_it_covers_no_particular_branch(self):
        """Null, not an empty string or the school's name. Every run raised
        before per-branch payroll existed is this shape, and the frontend has
        to be able to tell "the whole school" from "a site I cannot read"."""
        from vs_finance.payroll import generate_run_from_roster

        generate_run_from_roster(self.books, pay_date=datetime.date(2026, 1, 25))

        row = self._runs()[0]

        self.assertIsNone(row["branch_id"])
        self.assertIsNone(row["branch_name"])

    def test_two_runs_on_the_same_pay_date_are_distinguishable(self):
        """The failure this whole class exists for."""
        self._make(self.ikeja)
        self._make(self.lekki)

        rows = self._runs()

        self.assertEqual(len({r["pay_date"] for r in rows}), 1)
        self.assertEqual(
            sorted(r["branch_name"] for r in rows),
            ["Ikeja Branch", "Lekki Branch"],
        )

    def test_the_branch_name_costs_no_extra_query_per_run(self):
        """``select_related``, not a lazy relation walked once per row.

        Measured against one run rather than a fixed number, so the assertion
        reads "three runs cost what one run costs" and cannot drift when
        unrelated middleware adds a query of its own. A list that grows a query
        per item is how a screen that worked with three runs stops working with
        thirty.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._make(self.ikeja)
        with CaptureQueriesContext(connection) as one_run:
            self._runs()

        self._make(self.lekki)
        self._make(self.yaba)

        with self.assertNumQueries(len(one_run)):
            self._runs()

    # -- filtering ----------------------------------------------------------- #

    def test_the_runs_list_filters_by_branch(self):
        self._make(self.ikeja)
        self._make(self.lekki)

        rows = self._runs(query=f"&branch={self.ikeja.pk}")

        self.assertEqual([r["branch_name"] for r in rows], ["Ikeja Branch"])

    def test_the_runs_list_finds_the_central_ones(self):
        """``?branch=unassigned`` means the same thing on both payroll lists:
        the rows no branch owns. On the roster that is the people blocking a
        switch to per-branch payroll; here it is the central runs raised before
        the school switched."""
        from vs_finance.payroll import generate_run_from_roster

        generate_run_from_roster(self.books, pay_date=datetime.date(2026, 1, 25))
        self._make(self.ikeja)

        rows = self._runs(query="&branch=unassigned")

        self.assertEqual([r["branch_name"] for r in rows], [None])

    def test_a_branch_the_caller_cannot_work_in_is_refused_like_an_unknown_one(self):
        """Reported identically so the parameter cannot be used to enumerate a
        school's sites."""
        lekki_only = self.officer(
            self.tenant, "runs-lekki@fin.test", "runs-lekki", branches=[self.lekki],
        )

        response = lekki_only.get(
            f"/v1/finance/payroll-runs/?entity={self.books.code}&branch={self.ikeja.pk}",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("branch", str(response.data))

    def test_another_tenants_branch_is_refused_the_same_way(self):
        response = self.hq.get(
            f"/v1/finance/payroll-runs/?entity={self.books.code}"
            f"&branch={self.rival_branch.pk}",
        )

        self.assertEqual(response.status_code, 400, response.data)

    def test_the_roster_filter_still_works_through_the_shared_helper(self):
        """The roster's ``?branch=`` was moved onto the same helper as the runs
        list. It has to keep meaning exactly what it meant."""
        self.salary(self.books, "Unassigned Person")

        response = self.hq.get(
            f"/v1/finance/employee-salaries/?entity={self.books.code}&branch=unassigned",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["name"] for row in response.data["data"]], ["Unassigned Person"],
        )
