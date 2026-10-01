"""Record money the platform already holds for one branch when held custody starts tracking it.

The held-funds sub-ledger (:mod:`vs_payments.held`) starts at zero. Online money a
held-mode branch's payments put in the platform's provider balance before then is
not in it, so that branch's online payouts would be refused and its settlements
would never pay it on. A platform operator enters it from Paystack's records, once
per branch; a second opening balance for the same branch is refused:

    python manage.py record_held_opening_balance --tenant bright-star --branch 12 \\
        --amount 17800000 --by ada@codexng.com \\
        --reason "Paystack balance for Lekki at go-live, from the settlement report"

``--amount`` is kobo and ``--by`` the platform operator recording it, who must be a
user of the platform tenant. The movement is audited under that operator with the
reason, and posts Dr provider balance, Cr client funds held in the platform's
books.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Record an opening held balance for one branch of a held-mode tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="The tenant's slug.")
        parser.add_argument("--branch", required=True, type=int, help="The branch's id.")
        parser.add_argument("--amount", required=True, type=int, help="Kobo already held.")
        parser.add_argument("--by", required=True, help="Email of the platform operator recording it.")
        parser.add_argument("--reason", required=True, help="Where the figure comes from.")

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model

        from vs_finance.exceptions import FinanceError
        from vs_tenants.models import Branch, Tenant

        from vs_payments.held import record_opening_balance

        operator = get_user_model().objects.filter(
            email__iexact=options["by"].strip(), tenant__kind=Tenant.Kind.PLATFORM).first()
        if operator is None:
            raise CommandError(f"No platform operator with the email '{options['by']}'.")
        tenant = Tenant.objects.filter(slug=options["tenant"]).first()
        if tenant is None:
            raise CommandError(f"No tenant '{options['tenant']}'.")
        branch = Branch.all_objects.filter(pk=options["branch"], tenant=tenant).first()
        if branch is None:
            raise CommandError(f"No branch {options['branch']} at {tenant.slug}.")
        try:
            movement = record_opening_balance(
                tenant=tenant, branch=branch, amount=options["amount"],
                reason=options["reason"], actor_user=operator)
        except FinanceError as exc:
            raise CommandError(str(getattr(exc, "message", exc))) from exc
        posted = "posted" if movement.platform_journal_id else f"not posted ({movement.journal_error})"
        self.stdout.write(
            f"Recorded {options['amount']} kobo held for {branch.name}; platform journal {posted}.")
