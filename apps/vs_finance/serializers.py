"""DRF serializers for the vs_finance read/action API.

These serialise the ledger's master data and documents (entities, accounts, fiscal
periods, journals, invoices). The financial **reports/statements** are dataclasses,
not models, so the views render those to plain dicts directly (see ``reports_api`` in
:mod:`vs_finance.views`) rather than through a ModelSerializer.

Money is always kobo (integer minor units); each money field is mirrored with a
``*_naira`` display string so a client never has to know the divisor.
"""
from __future__ import annotations

from rest_framework import serializers

from core.media import signed_url
from vs_rbac.field_enforcement import FieldAccessMixin, can_read, visible

from .models import (
    Account,
    BankAccount,
    BankReconciliation,
    BankStatement,
    BankStatementLine,
    Budget,
    BudgetLine,
    Concession,
    CostCenter,
    CreditNote,
    CreditNoteLine,
    Currency,
    Customer,
    CustomerCreditTransfer,
    CustomerDeposit,
    Dimension,
    DoubtfulDebtProvision,
    DoubtfulDebtProvisionLine,
    DepreciationSchedule,
    DunningNotice,
    DunningPolicy,
    DunningStage,
    ExpenseClaim,
    ExpenseClaimLine,
    FeeItem,
    FeeStructure,
    FinanceAuditLog,
    FinanceDocumentDelivery,
    FixedAsset,
    FiscalPeriod,
    FiscalYear,
    FxRate,
    Invoice,
    JournalEntry,
    JournalLine,
    LedgerEntity,
    Payment,
    PaymentPlan,
    PaymentPlanInstallment,
    EmployeeSalary,
    PayrollLine,
    PayrollRun,
    PayrollRunBranch,
    StaffBankAccount,
    SalaryComponent,
    PayrollLineItem,
    EmployeeDeduction,
    EmployeeSalaryVersion,
    PayeTaxBand,
    PayeTaxRelief,
    PayeTaxTable,
    PayrollDeductionType,
    PayrollTaxJurisdiction,
    PensionFundAdministrator,
    Payslip,
    PayBroughtForward,
    SalaryStructure,
    PettyCashFund,
    PettyCashReturn,
    PettyCashVoucher,
    PettyCashVoucherLine,
    Refund,
    TaxCode,
    TaxFiling,
    TaxFilingShare,
    TaxObligation,
    TaxRemittance,
    WriteOffRequest,
)
from .money import format_naira


class LedgerEntitySerializer(serializers.ModelSerializer):
    """One set of books. ``kind_label`` is the kind in words, for a screen to show."""

    base_currency = serializers.CharField(source="base_currency_id", read_only=True)
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    # Originating school id derived from the tenant's school profile (None for
    # platform/product entities). Key kept stable for the frontend.
    source_school_id = serializers.SerializerMethodField()

    class Meta:
        model = LedgerEntity
        fields = [
            "id", "code", "number_code", "name", "kind", "kind_label", "base_currency",
            "is_active", "source_school_id",
        ]

    def get_source_school_id(self, obj):
        school = getattr(obj.tenant, "school_profile", None)
        return school.id if school else None


class LedgerEntityCreateSerializer(serializers.ModelSerializer):
    """Write serializer for provisioning a new set of books (super-admin only).

    ``code`` is normalised to uppercase (it appears verbatim inside every document
    number) and ``base_currency`` accepts the 3-letter currency code (its PK).
    """

    base_currency = serializers.PrimaryKeyRelatedField(
        queryset=Currency.objects.all(), required=False,
    )
    # Optional: which fiscal year to open. Defaults to the current calendar year.
    fiscal_year = serializers.IntegerField(required=False, write_only=True, min_value=2000)
    # Optional opening month (1–12). 1 = calendar Jan–Dec; 9 = a Sept–Aug school year.
    fiscal_start_month = serializers.IntegerField(
        required=False, write_only=True, min_value=1, max_value=12,
    )
    fiscal_period_frequency = serializers.ChoiceField(
        choices=(("MONTHLY", "Monthly"), ("QUARTERLY", "Quarterly")),
        required=False,
        write_only=True,
        default="MONTHLY",
    )
    fiscal_start_day = serializers.IntegerField(
        required=False, write_only=True, min_value=1, max_value=31, default=1,
    )

    class Meta:
        model = LedgerEntity
        fields = ["id", "code", "number_code", "name", "kind", "base_currency",
                  "fiscal_year", "fiscal_start_month", "fiscal_period_frequency",
                  "fiscal_start_day"]
        extra_kwargs = {
            "kind": {"required": False},
            # Optional: leave blank and the model auto-derives a unique short code.
            "number_code": {"required": False},
        }

    def validate_code(self, value):
        code = (value or "").strip().upper()
        if not code:
            raise serializers.ValidationError("Entity code is required.")
        if LedgerEntity.objects.filter(code=code).exists():
            raise serializers.ValidationError(f"A ledger entity with code '{code}' already exists.")
        return code

    def validate_number_code(self, value):
        code = (value or "").strip().upper()
        if not code:
            return ""  # blank → model save() auto-derives a unique code
        if len(code) > 3:
            raise serializers.ValidationError("Number code must be at most 3 characters.")
        if LedgerEntity.objects.filter(number_code=code).exists():
            raise serializers.ValidationError(f"Number code '{code}' is already in use.")
        return code

    def create(self, validated_data):
        from .provisioning import provision_books

        fiscal_year = validated_data.pop("fiscal_year", None)
        start_month = validated_data.pop("fiscal_start_month", 1)
        period_frequency = validated_data.pop("fiscal_period_frequency", "MONTHLY")
        start_day = validated_data.pop("fiscal_start_day", 1)

        # The owning tenant comes from the asserted request context; the entity
        # save() falls back to the codex platform tenant when none is present.
        request = self.context.get("request")
        request_tenant = getattr(request, "tenant", None) if request else None
        tenant = validated_data.get("tenant") or request_tenant

        # Everything that makes a set of books usable now lives in one service,
        # so this endpoint is a caller of it rather than the only door to it.
        return provision_books(
            tenant=tenant,
            name=validated_data.get("name"),
            code=validated_data.get("code"),
            base_currency=validated_data.get("base_currency"),
            kind=validated_data.get("kind") or LedgerEntity.Kind.TENANT,
            number_code=validated_data.get("number_code", ""),
            fiscal_year=fiscal_year,
            fiscal_start_month=start_month,
            fiscal_period_frequency=period_frequency,
            fiscal_start_day=start_day,
        )

    def to_representation(self, instance):
        # Echo back the canonical read shape so the caller sees base_currency code.
        return LedgerEntitySerializer(instance, context=self.context).data


class AccountSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """A chart-of-accounts row.

    ``managed_by`` is null for an account a person works with and
    ``{"keeper", "moved_by", "hand_journals"}`` for one the software keeps
    (:func:`vs_finance.account_guide.managed_accounts`). The view builds that map
    once and passes it as ``context["managed_accounts"]``; a serializer given no
    map answers null instead of querying per row.

    ``bank_account_id`` and ``bank_branch_id`` name the bank account behind a
    ledger account and that account's branch, empty for an account no bank
    account backs. A document is deposited into, or paid from, its own branch's
    bank only, so a deposit picker offering ledger accounts narrows by
    ``bank_branch_id`` to the document's branch instead of offering Lekki's
    collection ledger on an Ikeja receipt and meeting the refusal on save.
    """

    field_resource = "finance.account"

    parent_code = serializers.CharField(source="parent.code", read_only=True, default=None)
    bank_account_id = serializers.SerializerMethodField()
    bank_branch_id = serializers.SerializerMethodField()
    # Net GL balance signed to the account's normal balance - populated from the
    # ``_bal_dr``/``_bal_cr`` annotations the chart-of-accounts view adds.
    balance = serializers.SerializerMethodField()
    # Sub-ledger role: AR/AP control account, or the cash & bank account.
    tag = serializers.SerializerMethodField()
    managed_by = serializers.SerializerMethodField()

    class Meta:
        model = Account
        fields = [
            "id", "code", "name", "account_type", "normal_balance",
            "is_contra", "is_postable", "is_active", "parent_id", "parent_code",
            "subtype", "description", "ifrs_line", "balance", "tag", "managed_by",
            "bank_account_id", "bank_branch_id",
        ]

    @staticmethod
    def _bank(obj):
        from django.core.exceptions import ObjectDoesNotExist

        try:
            return obj.bank_account
        except ObjectDoesNotExist:
            return None

    def get_bank_account_id(self, obj):
        return getattr(self._bank(obj), "pk", None)

    def get_bank_branch_id(self, obj):
        return getattr(self._bank(obj), "branch_id", None)

    def get_balance(self, obj):
        from .constants import NormalBalance

        dr = getattr(obj, "_bal_dr", None)
        cr = getattr(obj, "_bal_cr", None)
        if dr is None and cr is None:
            return None  # not annotated (e.g. picker queries) - omit
        net = (dr or 0) - (cr or 0)
        if obj.normal_balance != NormalBalance.DEBIT:
            net = -net
        return {"kobo": int(net), "naira": format_naira(int(net))}

    def get_tag(self, obj):
        if obj.id in self.context.get("control_ids", set()):
            return "CONTROL"
        if obj.id in self.context.get("cash_ids", set()):
            return "CASH"
        return None

    def get_managed_by(self, obj):
        from .account_guide import managed_by_payload

        return managed_by_payload(self.context.get("managed_accounts", {}).get(obj.id))

    def to_representation(self, instance):
        """The account, without what restates a bank account the caller may not read.

        A ledger account behind a bank account carries that account's book
        balance as its own, and ``bank_account_id`` is the registered link
        between them (``finance.bankaccount.ledger_account``). The chart
        therefore drops each when its switch is closed, so a role that cannot
        read a bank account's balance or its ledger account cannot learn either
        from the chart of accounts.
        """
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request is not None:
            if self._bank(instance) is not None and not can_read(
                request, "finance.bankaccount.ledger_account"
            ):
                data.pop("bank_account_id", None)
            if instance.pk in self.context.get("bank_balance_hidden_ids", ()) or (
                self._bank(instance) is not None
                and not can_read(request, "finance.bankaccount.book_balance")
            ):
                data.pop("balance", None)
        return data


class AccountSummarySerializer(FieldAccessMixin, serializers.Serializer):
    """Account balances and row counts for one reader's branch reach."""

    field_resource = "finance.account"

    current_balance = serializers.DictField(read_only=True)
    opening_balance = serializers.DictField(read_only=True)
    line_count = serializers.IntegerField(read_only=True)
    journal_count = serializers.IntegerField(read_only=True)
    fiscal_year_start = serializers.CharField(read_only=True, allow_null=True)
    as_of = serializers.CharField(read_only=True)


class AccountActivityRowSerializer(FieldAccessMixin, serializers.Serializer):
    """A posted ledger line, with money controlled by the account balance switch."""

    field_resource = "finance.account"

    id = serializers.IntegerField(read_only=True)
    date = serializers.CharField(read_only=True)
    account_id = serializers.IntegerField(read_only=True)
    account_code = serializers.CharField(read_only=True)
    account_name = serializers.CharField(read_only=True)
    journal_no = serializers.CharField(read_only=True)
    source = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True, allow_blank=True)
    cost_center = serializers.CharField(read_only=True, allow_blank=True)
    dimensions = serializers.DictField(read_only=True)
    debit = serializers.DictField(read_only=True)
    credit = serializers.DictField(read_only=True)
    running_balance = serializers.DictField(read_only=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if self.context.get("bank_balance_hidden"):
            for name in ("debit", "credit", "running_balance"):
                data.pop(name, None)
        return data


class AccountActivityTotalsSerializer(FieldAccessMixin, serializers.Serializer):
    """Ledger movement totals controlled by the account balance switch."""

    field_resource = "finance.account"

    debit = serializers.DictField(read_only=True)
    credit = serializers.DictField(read_only=True)
    net_movement = serializers.DictField(read_only=True)


class AccountDetailEnvelopeSerializer(FieldAccessMixin, serializers.Serializer):
    """The account, scoped balances and ledger activity as one field-aware payload."""

    field_resource = "finance.account"

    account = AccountSerializer(read_only=True)
    type_label = serializers.CharField(read_only=True, allow_blank=True)
    summary = AccountSummarySerializer(read_only=True)
    activity = AccountActivityRowSerializer(many=True, read_only=True)


class BranchStatesMixin:
    """Add ``branch_states`` when the view primed them (``context["branch_states"]``).

    The key is absent otherwise, so every other reader of a period or year gets
    exactly the shape it always had (:func:`vs_finance.views.calendar_branch_states`).
    """

    def to_representation(self, instance):
        data = super().to_representation(instance)
        states = self.context.get("branch_states")
        if states is not None:
            data["branch_states"] = states.get(instance.pk, [])
        return data


class PeriodLabelMixin:
    """Names a fiscal period the way a person reads it, reading each school once.

    :func:`label_period` is :func:`vs_finance.wording.period_label` for a period
    in this serializer's output: the month in words ("September 2026"), in the
    tenant that owns the books the period belongs to. The tenant is looked up once
    per set of books for the life of the serializer, and a serialized list shares
    one child serializer, so a page of rows costs one lookup however long it is.
    The tenant instance is also what remembers the school's date format, so the
    words of every row after the first cost nothing either.
    """

    def _tenant_of(self, entity_id):
        if not hasattr(self, "_tenants"):
            self._tenants = {}
        if entity_id not in self._tenants:
            self._tenants[entity_id] = (
                LedgerEntity.objects.select_related("tenant").get(pk=entity_id).tenant
            )
        return self._tenants[entity_id]

    def label_period(self, period) -> str | None:
        """``period`` in words, or ``None`` for a record that has no period."""
        from .wording import period_label

        if period is None:
            return None
        return period_label(period, self._tenant_of(period.entity_id))


class FiscalPeriodSerializer(
    FieldAccessMixin, PeriodLabelMixin, BranchStatesMixin, serializers.ModelSerializer,
):
    """A fiscal period; ``label`` is how a person picking it reads it.

    ``name`` is the stored name ("2026-09"), kept for a client that keys on it;
    ``label`` is the month in words ("September 2026", through
    :func:`vs_finance.wording.period_label`), what a picker or a list shows.

    Where the month stands in its close (``status``, ``closed_at`` and, when a
    list asks for them, ``branch_states``) is one switch of ``finance.period``.
    ``branch_states`` is declared so the registry can see it, and stays absent
    until the view primes it (:class:`BranchStatesMixin`).
    """

    field_resource = "finance.period"

    fiscal_year = serializers.IntegerField(source="fiscal_year.year", read_only=True)
    status = serializers.SerializerMethodField()
    label = serializers.SerializerMethodField()
    branch_states = serializers.ListField(read_only=True)

    class Meta:
        model = FiscalPeriod
        fields = [
            "id", "period_no", "name", "label", "fiscal_year",
            "start_date", "end_date", "status", "closed_at", "is_closing", "branch_states",
        ]

    def get_status(self, obj):
        return getattr(obj, "_branch_status", obj.status)

    def get_label(self, obj) -> str:
        return self.label_period(obj)


class FiscalYearSerializer(FieldAccessMixin, BranchStatesMixin, serializers.ModelSerializer):
    """A fiscal year; ``is_archived`` says whether it is put away (:mod:`vs_finance.archive`).

    ``status`` and ``branch_states`` are the year's close state and ``is_archived``
    and ``archived_at`` its archive state, one switch each in ``finance.fiscalyear``.
    ``branch_states`` is declared so the registry can see it, and stays absent
    until the view primes it (:class:`BranchStatesMixin`).
    """

    field_resource = "finance.fiscalyear"

    is_archived = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    branch_states = serializers.ListField(read_only=True)

    class Meta:
        model = FiscalYear
        fields = [
            "id", "year", "start_date", "end_date", "status", "is_archived", "archived_at",
            "branch_states",
        ]

    def get_is_archived(self, obj) -> bool:
        return obj.archived_at is not None

    def get_status(self, obj):
        return getattr(obj, "_branch_status", obj.status)


class ApprovalStateMixin(serializers.Serializer):
    """Adds read-only ``approval_state`` and ``approval_returned``: where the document stands with its route.

    ``approval_state`` is ``NOT_SUBMITTED`` (never sent, or its request was
    withdrawn or cancelled), ``PENDING`` (a request is open, including one an
    approver sent back to its sender), ``APPROVED`` or ``REJECTED``, read from the
    document's latest request (:func:`vs_finance.approvals.approval_overview`).

    ``approval_returned`` is True while an approver has handed the document back
    to whoever sent it: a draft still reading PENDING, because its request is
    open. Its sender corrects it through the document's edit route, where it has
    one, and resumes it from the approvals screen; its own submit and send
    routes refuse it (:func:`vs_finance.approvals.correcting_returned`). False
    in every other state.

    Read once per page, not once per row: the first row of a list reads the
    whole page in one query and caches it in the shared serializer context under
    the document's model, so documents of two kinds in one response never mix.
    A single document costs one query, for both fields.
    """

    approval_state = serializers.SerializerMethodField()
    approval_returned = serializers.SerializerMethodField()

    def _approval(self, obj) -> tuple:
        from .approvals import approval_overview

        model = type(obj)
        overview = self.context.setdefault(f"approval_overview:{model._meta.label_lower}", {})
        if obj.pk not in overview:
            rows = [obj]
            parent = self.parent
            if isinstance(parent, serializers.ListSerializer) and parent.instance is not None:
                from django.db.models import Manager

                listed = parent.instance
                listed = listed.all() if isinstance(listed, Manager) else listed
                page = [row for row in listed if type(row) is model]
                rows = page if any(row.pk == obj.pk for row in page) else rows
            overview.update(approval_overview(rows))
        return overview[obj.pk]

    def get_approval_state(self, obj) -> str:
        return self._approval(obj)[0]

    def get_approval_returned(self, obj) -> bool:
        return self._approval(obj)[1]


class JournalLineSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.journal"
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    debit_naira = serializers.SerializerMethodField()
    credit_naira = serializers.SerializerMethodField()

    class Meta:
        model = JournalLine
        fields = [
            "id", "line_no", "account_id", "account_code", "account_name",
            "cost_center", "dimensions", "debit", "credit", "debit_naira", "credit_naira",
            "description",
        ]

    def get_debit_naira(self, obj) -> str:
        return format_naira(obj.debit)

    def get_credit_naira(self, obj) -> str:
        return format_naira(obj.credit)


class JournalPeopleListSerializer(serializers.ListSerializer):
    """Resolve the people who raised a page of journals in one lookup."""

    def to_representation(self, data):
        from core.person_exit import prime_exit_states

        rows = list(data)
        prime_exit_states(self.context, (row.created_by_id for row in rows))
        return super().to_representation(rows)


class JournalEntryListSerializer(FieldAccessMixin, PeriodLabelMixin, ApprovalStateMixin,
                                 serializers.ModelSerializer):
    """A journal in a list. ``created_by_is_exited`` is true once its maker has left.

    ``null`` for a journal the system raised with nobody named. ``period`` is the
    period's stored name ("2026-09"), kept for a client that keys on it, and
    ``period_label`` the month in words ("September 2026"), what a screen shows;
    both are ``null`` for a journal with no period.
    """

    field_resource = "finance.journal"

    period = serializers.CharField(source="period.name", read_only=True, default=None)
    period_label = serializers.SerializerMethodField()
    total_debit = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    created_by_id = serializers.IntegerField(read_only=True, default=None)
    created_by_is_exited = serializers.SerializerMethodField()
    opening_balance = serializers.SerializerMethodField()

    class Meta:
        model = JournalEntry
        fields = [
            "id", "document_number", "date", "period", "period_label", "source",
            "status", "narration", "reference", "posted_at",
            "total_debit", "created_by", "created_by_id", "created_by_is_exited",
            "approval_state", "approval_returned", "opening_balance",
        ]
        list_serializer_class = JournalPeopleListSerializer

    def get_period_label(self, obj) -> str | None:
        return self.label_period(obj.period)

    def get_opening_balance(self, obj) -> bool:
        from .constants import JournalSource

        return obj.source == JournalSource.OPENING

    def get_created_by_is_exited(self, obj):
        from core.person_exit import person_is_exited

        return person_is_exited(self.context, obj.created_by_id)

    def get_total_debit(self, obj) -> int:
        # The list view annotates `_total_debit` (one query); detail falls back to totals().
        val = getattr(obj, "_total_debit", None)
        return int(val) if val is not None else obj.totals()[0]

    def get_created_by(self, obj) -> str:
        u = getattr(obj, "created_by", None)
        if u is None:
            return "system"
        name = f"{getattr(u, 'first_name', '') or ''} {getattr(u, 'last_name', '') or ''}".strip()
        return name or getattr(u, "email", "") or "system"


class JournalEntryDetailSerializer(JournalEntryListSerializer):
    lines = JournalLineSerializer(many=True, read_only=True)
    total_credit = serializers.SerializerMethodField()
    reversal_action = serializers.SerializerMethodField()

    class Meta(JournalEntryListSerializer.Meta):
        fields = JournalEntryListSerializer.Meta.fields + [
            "lines", "total_credit", "reverses_id", "reversal_action",
        ]

    def _totals(self, obj):
        cache = getattr(self, "_totals_cache", {})
        if obj.id not in cache:
            cache[obj.id] = obj.totals()
            self._totals_cache = cache
        return cache[obj.id]

    def get_total_credit(self, obj) -> int:
        return self._totals(obj)[1]

    def get_reversal_action(self, obj) -> dict:
        from .posting import journal_reversal_action

        return journal_reversal_action(obj)


class JournalSummarySerializer(FieldAccessMixin, serializers.Serializer):
    """List-screen counts and totals under the journal's field switches."""

    field_resource = "finance.journal"

    total = serializers.IntegerField()
    by_status = serializers.DictField()
    sent_back = serializers.IntegerField()
    posted_total = serializers.DictField()
    reversed_total = serializers.DictField()


class DirectEntryLineSerializer(serializers.Serializer):
    """One line of a direct entry: an account and a one-sided amount (kobo)."""

    account = serializers.CharField(help_text="Account code within the entity, e.g. '1100'.")
    debit = serializers.IntegerField(required=False, default=0, min_value=0)
    credit = serializers.IntegerField(required=False, default=0, min_value=0)
    cost_center = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="Optional cost-centre code (or id) to slice this line by; resolved "
                  "within the entity. Carried onto the GL line.",
    )
    dimensions = serializers.DictField(
        child=serializers.CharField(), required=False, default=dict,
        help_text="Optional analytical values keyed by Dimension.code, e.g. "
                  "{'FUND': 'GRANT-A'}. Each value must be an allowed value of that axis.",
    )

    def validate(self, attrs):
        if attrs.get("debit") and attrs.get("credit"):
            raise serializers.ValidationError(
                "A line is one-sided: set either debit or credit, not both.")
        return attrs


class DirectEntryCreateSerializer(serializers.Serializer):
    """Write serializer for a direct journal entry (capital, loans, openings, adjustments).

    All amounts are integer minor units (kobo). The lines must balance.
    """

    date = serializers.DateField(required=False)
    narration = serializers.CharField(required=False, allow_blank=True, default="")
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    branch = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        help_text="Branch id or code the entry belongs to. A caller bound to one "
                  "branch may leave it out; a caller bound to several, or a "
                  "whole-tenant caller at a school with several branches, must name "
                  "one. At a school with one branch it may be left out.",
    )
    lines = DirectEntryLineSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("At least one line is required.")
        debit = sum(line["debit"] for line in value)
        credit = sum(line["credit"] for line in value)
        if debit != credit:
            raise serializers.ValidationError(
                f"Entry must balance: debits {format_naira(debit)} against credits "
                f"{format_naira(credit)}.")
        if debit == 0:
            raise serializers.ValidationError("Direct entry total cannot be zero.")
        return value


class CustomerSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One customer / payer (the AR sub-ledger party), under ``finance.customer``.

    ``code`` and ``name`` identify the customer wherever it is named (pickers,
    documents, messages) and carry no switch. Every other field is registered:
    a caller whose roles cannot read one gets the customer without it, and the
    create and update endpoints refuse a role that cannot change it.

    ``branch_id`` is the branch the customer is filed under, or empty for one
    every branch shares. A document raised against a filed customer takes its
    branch; one raised against a shared customer names its own, so a form asks
    for a branch only when this is empty.
    """

    field_resource = "finance.customer"
    field_access_detail = True
    field_write_aliases = True

    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    receivable_account_code = serializers.CharField(
        source="receivable_account.code", read_only=True, default=None)
    receivable_account_name = serializers.CharField(
        source="receivable_account.name", read_only=True, default=None)
    opening_balance_naira = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "id", "code", "name", "billing_email", "billing_phone", "billing_address",
            "receivable_account_id", "receivable_account_code", "receivable_account_name",
            "opening_balance", "opening_balance_naira", "source_type", "source_id",
            "is_active", "branch_id", "branch_name", "created_at",
        ]

    def get_opening_balance_naira(self, obj) -> str:
        return format_naira(obj.opening_balance)


class CustomerListSerializer(CustomerSerializer):
    """A row of the customers list: the customer and where its account stands.

    The standing comes from maps the list view works out once for the page
    (``standing``: customer id to ledger figures, ``pays_for``: customer id to
    the count of customers it pays for), so the page costs two queries however
    many rows it holds.
    """

    field_access_detail = False

    balance = serializers.SerializerMethodField()
    balance_naira = serializers.SerializerMethodField()
    account_status = serializers.SerializerMethodField()
    pays_for_count = serializers.SerializerMethodField()

    class Meta(CustomerSerializer.Meta):
        fields = CustomerSerializer.Meta.fields + [
            "balance", "balance_naira", "account_status", "pays_for_count",
        ]

    def _net(self, obj) -> int:
        led = (self.context.get("standing") or {}).get(obj.pk, {})
        return led.get("outstanding", 0) - led.get("credit", 0)

    def get_balance(self, obj) -> int:
        return self._net(obj)

    def get_balance_naira(self, obj) -> str:
        return format_naira(self._net(obj))

    def get_account_status(self, obj) -> str:
        from .views_ar import _account_status

        led = (self.context.get("standing") or {}).get(obj.pk, {})
        return _account_status(self._net(obj), led.get("overdue", False))

    def get_pays_for_count(self, obj) -> int:
        return (self.context.get("pays_for") or {}).get(obj.pk, 0)


class CustomerStandingSerializer(FieldAccessMixin, serializers.Serializer):
    """The summary block of a customer's detail: where the account stands."""

    field_resource = "finance.customer"

    current_balance = serializers.ReadOnlyField()
    lifetime_paid = serializers.ReadOnlyField()
    open_invoice_count = serializers.ReadOnlyField()
    account_status = serializers.ReadOnlyField()


class CustomerAccountSerializer(FieldAccessMixin, serializers.Serializer):
    """A customer's detail response: the customer, its standing and its account history.

    The history blocks are the customer's documents as the account shows
    them. A statement line's running ``balance`` repeats the customer's
    balance, so it leaves the statement when the balance is hidden (the view
    builds the lines that way), and a standing block with nothing left in it
    is dropped rather than served empty.
    """

    field_resource = "finance.customer"

    customer = CustomerSerializer(read_only=True)
    summary = CustomerStandingSerializer(read_only=True)
    open_invoices = serializers.ReadOnlyField()
    open_debit_notes = serializers.ReadOnlyField()
    transactions = serializers.ReadOnlyField()
    statement = serializers.ReadOnlyField()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "summary" in data and not data["summary"]:
            data.pop("summary")
        return data


class CustomerSummaryTotalsSerializer(FieldAccessMixin, serializers.Serializer):
    """The header cards of the customers screen, under ``finance.customer``.

    ``total`` counts the customers the list holds and is not a switch.
    ``status_counts`` counts inactive customers beside the account statuses,
    so it needs both the account status and the active flag.
    """

    field_resource = "finance.customer"

    total = serializers.ReadOnlyField()
    receivable = serializers.ReadOnlyField()
    on_credit = serializers.ReadOnlyField()
    overdue = serializers.ReadOnlyField()
    status_counts = serializers.ReadOnlyField()

    def to_representation(self, instance):
        from vs_rbac.field_enforcement import can_read

        data = super().to_representation(instance)
        request = self.context.get("request")
        if "status_counts" in data and not can_read(request, "finance.customer.is_active"):
            data.pop("status_counts")
        return data


class FeeItemSerializer(serializers.ModelSerializer):
    revenue_account_code = serializers.CharField(source="revenue_account.code", read_only=True)
    tax_code_value = serializers.CharField(source="tax_code.code", read_only=True, default=None)
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = FeeItem
        fields = [
            "id", "line_no", "code", "description", "revenue_account_code",
            "amount", "amount_naira", "tax_code_value", "is_optional", "kind",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


class FeeStructurePeopleListSerializer(serializers.ListSerializer):
    """Resolve fee-structure creators once for a page."""

    def to_representation(self, data):
        from core.person_exit import prime_exit_states

        rows = list(data)
        prime_exit_states(self.context, (row.created_by_id for row in rows))
        return super().to_representation(rows)


class FeeStructureSerializer(serializers.ModelSerializer):
    items = FeeItemSerializer(many=True, read_only=True)
    total = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    tax_total = serializers.IntegerField(read_only=True)
    tax_total_naira = serializers.SerializerMethodField()
    total_with_tax = serializers.IntegerField(read_only=True)
    total_with_tax_naira = serializers.SerializerMethodField()
    applies_to_display = serializers.CharField(
        source="get_applies_to_display", read_only=True)
    # The branch whose price list this is; null for a template shared across the
    # tenant. A screen billing from it keeps to that branch. The id rather than
    # a nested name, so the list stays one query.
    branch_id = serializers.IntegerField(read_only=True, allow_null=True)
    # Usage/activity - only computed for the detail view (context with_usage=True),
    # so the list endpoint stays a single query per page.
    created_by_name = serializers.SerializerMethodField()
    created_by_is_exited = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()

    class Meta:
        model = FeeStructure
        list_serializer_class = FeeStructurePeopleListSerializer
        fields = [
            "id", "code", "name", "applies_to", "applies_to_display",
            "branch_id", "description", "is_active", "items",
            "total", "total_naira", "tax_total", "tax_total_naira",
            "total_with_tax", "total_with_tax_naira",
            "created_at", "created_by_name", "created_by_is_exited", "usage",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def get_tax_total_naira(self, obj) -> str:
        return format_naira(obj.tax_total)

    def get_total_with_tax_naira(self, obj) -> str:
        return format_naira(obj.total_with_tax)

    def get_created_by_name(self, obj):
        u = obj.created_by
        if not u:
            return None
        name = " ".join(filter(None, [
            getattr(u, "first_name", ""), getattr(u, "last_name", "")])).strip()
        return name or getattr(u, "email", None)

    def get_created_by_is_exited(self, obj):
        from core.person_exit import person_is_exited

        return person_is_exited(self.context, obj.created_by_id)

    def get_usage(self, obj):
        """Invoices raised from this structure (reference 'FEE:<code>'). Detail only."""
        if not self.context.get("with_usage"):
            return None
        from .models import Invoice
        qs = Invoice.objects.filter(
            entity_id=obj.entity_id, reference=f"FEE:{obj.code}", status="POSTED")
        last = qs.order_by("-created_at").values_list("created_at", flat=True).first()
        return {"invoices_generated": qs.count(), "last_generated_at": last}


class InvoiceSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """Read shape for a sales invoice.

    ``branch_id`` is the invoice's own branch, which is not always its
    customer's: an invoice raised against a customer every branch shares names
    the branch that raised it. A receipt against the invoice takes that branch
    and is deposited only into that branch's bank accounts, so a payment form
    narrows its deposit picker by this rather than by the customer's.
    """

    field_resource = "finance.invoice"

    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    settled_amount = serializers.IntegerField(read_only=True)
    balance_due = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    # Set when the customer billed pays for somebody else (a sponsor or employer).
    beneficiary_code = serializers.CharField(
        source="beneficiary.code", read_only=True, default=None)
    beneficiary_name = serializers.CharField(
        source="beneficiary.name", read_only=True, default=None)
    currency_code = serializers.CharField(source="currency.code", read_only=True, default=None)

    class Meta:
        model = Invoice
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "beneficiary_id", "beneficiary_code", "beneficiary_name",
            "branch_id", "branch_name",
            "invoice_date", "due_date", "status", "payment_status",
            "subtotal", "tax_total", "total", "total_naira",
            "amount_paid", "amount_credited", "settled_amount", "balance_due",
            "reference", "narration", "billing_period", "billing_period_label",
            "currency_code",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "total" not in data:
            for name in ("amount_paid", "amount_credited", "settled_amount", "balance_due"):
                data.pop(name, None)
        return data


class InvoiceDetailSummarySerializer(FieldAccessMixin, serializers.Serializer):
    """The invoice's totals and settlement figures in display money shape."""

    field_resource = "finance.invoice"

    subtotal = serializers.DictField(read_only=True)
    tax = serializers.DictField(read_only=True)
    total = serializers.DictField(read_only=True)
    paid = serializers.DictField(read_only=True)
    credited = serializers.DictField(read_only=True)
    settled = serializers.DictField(read_only=True)
    balance = serializers.DictField(read_only=True)
    due_date = serializers.CharField(read_only=True, allow_null=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "total" not in data:
            for name in ("paid", "credited", "settled", "balance"):
                data.pop(name, None)
        return data


class InvoiceDetailLineSerializer(FieldAccessMixin, serializers.Serializer):
    """One invoice line, with unit price separate from its other details."""

    field_resource = "finance.invoice"

    description = serializers.CharField(read_only=True)
    account_code = serializers.CharField(read_only=True)
    account_name = serializers.CharField(read_only=True)
    quantity = serializers.CharField(read_only=True)
    unit_price = serializers.DictField(read_only=True)
    tax_code = serializers.CharField(read_only=True, allow_null=True)
    tax_amount = serializers.DictField(read_only=True)
    line_total = serializers.DictField(read_only=True)
    kind = serializers.CharField(read_only=True)
    service_start = serializers.CharField(read_only=True, allow_null=True)
    service_end = serializers.CharField(read_only=True, allow_null=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "unit_price" not in data:
            data.pop("tax_amount", None)
            data.pop("line_total", None)
        return data


class InvoiceDetailEnvelopeSerializer(FieldAccessMixin, serializers.Serializer):
    """The invoice drawer's nested blocks under their owning switches."""

    field_resource = "finance.invoice"

    invoice = InvoiceSerializer(read_only=True)
    summary = InvoiceDetailSummarySerializer(read_only=True)
    lines = InvoiceDetailLineSerializer(many=True, read_only=True)
    deferred_income = serializers.JSONField(read_only=True)
    payments = serializers.JSONField(read_only=True)
    settlements = serializers.JSONField(read_only=True)
    gl_postings = serializers.JSONField(read_only=True)
    gl_journals = serializers.JSONField(read_only=True)
    reminders = serializers.JSONField(read_only=True)
    activity = serializers.JSONField(read_only=True)


class InvoiceSummarySerializer(FieldAccessMixin, serializers.Serializer):
    """Invoice list figures and status counts under their switches."""

    field_resource = "finance.invoice"

    kpis = serializers.JSONField(read_only=True)
    by_status = serializers.JSONField(read_only=True)
    totals = serializers.JSONField(read_only=True)
    monthly = serializers.JSONField(read_only=True)


class CreditNoteLineSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.creditnote"
    revenue_account = serializers.CharField(source="revenue_account.code", read_only=True)
    tax_code = serializers.CharField(source="tax_code.code", read_only=True, default=None)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)

    class Meta:
        model = CreditNoteLine
        fields = [
            "id", "line_no", "description", "revenue_account",
            "quantity", "unit_price", "tax_code", "net_amount", "tax_amount",
            "cost_center",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "unit_price" not in data:
            data.pop("net_amount", None)
            data.pop("tax_amount", None)
        return data


class ApprovalGatedMixin(serializers.Serializer):
    """Adds a read-only ``approval_required`` to an adjustment document read.

    Without it a client cannot tell whether to offer **Post** or **Submit for
    approval** except by refetching the workflow templates and reimplementing the
    branch → tenant → platform cascade itself, which is a second copy of the gate
    that drifts the first time the cascade changes. Same
    :class:`~vs_finance.approvals.ApprovalGate` the post views consult, so the
    button a client renders and the answer it gets back always agree.

    The gate is cached on the serializer instance rather than rebuilt per row:
    ``many=True`` reuses one child serializer for the whole list, so a page of
    refunds costs one template lookup for their shared scope, not one each.
    """

    approval_required = serializers.SerializerMethodField()

    # Report whether this document must go through approval before it posts.
    def get_approval_required(self, obj) -> bool:
        from .approvals import ApprovalGate

        gate = self.context.get("approval_gate")  # A view batching several reads may pass its own.
        if gate is None:
            gate = getattr(self, "_approval_gate", None)
            if gate is None:
                gate = ApprovalGate()
                self._approval_gate = gate
        return gate.required(obj)


def prime_income_given_back(context: dict, journal_ids) -> dict:
    """Cache, once per response, the income each adjusting journal gave back to other branches.

    ``{journal_id: [row, ...]}`` for the ids not cached yet, read in one query.
    Each row is an ``INCOME_GIVEN_BACK`` transfer whose ``adjustment_entry`` is
    that journal (:func:`vs_finance.inter_branch.book_income_given_back`):
    ``id``, ``document_number``, ``to_branch_id``, ``to_branch_name`` (the branch
    that booked the income and now gives it back), ``amount`` and ``status``. A
    voided credit note's transfers stay listed as ``REVERSED``.
    """
    from .constants import InterBranchTransferKind
    from .models import InterBranchTransfer

    cached = context.setdefault("income_given_back", {})
    missing = {int(pk) for pk in journal_ids if pk is not None} - cached.keys()
    if not missing:
        return cached
    cached.update({pk: [] for pk in missing})
    transfers = (
        InterBranchTransfer.objects
        .filter(adjustment_entry_id__in=missing, kind=InterBranchTransferKind.INCOME_GIVEN_BACK)
        .select_related("to_branch")
        .order_by("pk")
    )
    for transfer in transfers:
        cached[transfer.adjustment_entry_id].append({
            "id": transfer.pk,
            "document_number": transfer.document_number,
            "to_branch_id": transfer.to_branch_id,
            "to_branch_name": transfer.to_branch.name,
            "amount": int(transfer.amount),
            "status": transfer.status,
        })
    return cached


class IncomeGivenBackListSerializer(serializers.ListSerializer):
    """Read every listed document's income given back in one query for the page."""

    def to_representation(self, data):
        rows = list(data)
        prime_income_given_back(self.context, (row.journal_id for row in rows))
        return super().to_representation(rows)


class IncomeGivenBackMixin(serializers.Serializer):
    """``income_given_back`` on a credit note or concession raised on a moved bill.

    When Lekki credits Tunde's textbook bill that Ikeja booked before he moved,
    Ikeja's revenue and VAT come down through an inter-branch transfer linked to
    the credit note's journal. These rows name each such transfer, so the
    document screen can link to it. Empty for a document that took nothing from
    another branch, and for a draft. The transfers are party to the document's
    own branch, so whoever reads the document may read them.

    Each row restates a transfer of the registered ``finance.interbranch``
    resource, so its amount and status are served only to a caller who may read
    them; the transfer's number and the branch it was given back to identify it
    and always show.
    """

    income_given_back = serializers.SerializerMethodField()

    def get_income_given_back(self, obj) -> list:
        if obj.journal_id is None:
            return []
        rows = prime_income_given_back(self.context, (obj.journal_id,))[obj.journal_id]
        request = self.context.get("request")
        return [visible(request, "finance.interbranch", row) for row in rows]


class CreditNoteSerializer(FieldAccessMixin, IncomeGivenBackMixin, ApprovalGatedMixin, ApprovalStateMixin,
                           serializers.ModelSerializer):
    field_resource = "finance.creditnote"
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.document_number", read_only=True, default=None)
    unallocated_amount = serializers.IntegerField(read_only=True)
    credit_remaining = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    lines = CreditNoteLineSerializer(many=True, read_only=True)

    class Meta:
        model = CreditNote
        fields = [
            "id", "document_number", "kind", "customer_id", "customer_code",
            "customer_name", "invoice_id", "invoice_number", "note_date", "status",
            "subtotal", "tax_total", "total", "total_naira",
            "allocated_amount", "unallocated_amount", "refunded_amount",
            "transferred_amount", "credit_remaining", "reason", "reference", "lines",
            "approval_required", "income_given_back", "approval_state", "approval_returned",
        ]
        list_serializer_class = IncomeGivenBackListSerializer

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def to_representation(self, instance):
        """The credit note, without the customer's credit left once their balance is hidden.

        ``credit_remaining`` is what is still owed back to the customer, which
        is the customer's balance seen through one document.
        """
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request is not None and not can_read(request, "finance.customer.balance"):
            data.pop("credit_remaining", None)
        return data


class RefundSerializer(FieldAccessMixin, ApprovalGatedMixin, ApprovalStateMixin,
                       serializers.ModelSerializer):
    field_resource = "finance.refund"
    field_aliases = {"amount_naira": "amount", "approval_required": "status",
                     "approval_state": "status", "approval_returned": "status"}
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = Refund
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "refund_date", "method", "status", "amount", "amount_naira",
            "bank_account_id", "reference", "narration", "approval_required",
            "approval_state", "approval_returned",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


class CustomerCreditTransferSerializer(FieldAccessMixin, ApprovalGatedMixin, ApprovalStateMixin,
                                       serializers.ModelSerializer):
    """Read shape for a customer credit transfer and the receipt it gave its destination."""

    field_resource = "finance.credittransfer"

    from_customer_code = serializers.CharField(source="from_customer.code", read_only=True)
    from_customer_name = serializers.CharField(source="from_customer.name", read_only=True)
    to_customer_code = serializers.CharField(source="to_customer.code", read_only=True)
    to_customer_name = serializers.CharField(source="to_customer.name", read_only=True)
    receipt_number = serializers.CharField(
        source="receipt.document_number", read_only=True, default=None)
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = CustomerCreditTransfer
        fields = [
            "id", "document_number", "status", "branch_id",
            "from_customer_id", "from_customer_code", "from_customer_name",
            "to_customer_id", "to_customer_code", "to_customer_name",
            "transfer_date", "amount", "amount_naira", "reason",
            "receipt_id", "receipt_number", "approval_required", "approval_state",
            "approval_returned",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


class WriteOffRequestSerializer(FieldAccessMixin, ApprovalGatedMixin, ApprovalStateMixin,
                                serializers.ModelSerializer):
    field_resource = "finance.writeoff"
    invoice_number = serializers.CharField(source="invoice.document_number", read_only=True)
    customer_code = serializers.CharField(source="invoice.customer.code", read_only=True)
    customer_name = serializers.CharField(source="invoice.customer.name", read_only=True)
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = WriteOffRequest
        fields = [
            "id", "document_number", "status", "invoice_id", "invoice_number",
            "customer_code", "customer_name", "amount", "amount_naira",
            "write_off_account_id", "write_off_date", "narration", "reason",
            "journal_id", "approval_required", "allowance_used", "recovered_amount",
            "approval_state", "approval_returned",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


class PaymentSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """A customer receipt + its allocation state (for Receipts & Allocation)."""

    field_resource = "finance.payment"

    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    deposit_account_code = serializers.CharField(source="deposit_account.code", read_only=True, default=None)
    deposit_account_name = serializers.CharField(source="deposit_account.name", read_only=True, default=None)
    amount_naira = serializers.SerializerMethodField()
    unallocated_amount = serializers.IntegerField(read_only=True)
    credit_remaining = serializers.IntegerField(read_only=True)
    allocation_status = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "payment_date", "method", "amount", "amount_naira", "allocated_amount",
            "unallocated_amount", "refunded_amount", "transferred_amount", "credit_remaining",
            "allocation_status", "deposit_account_code",
            "deposit_account_name", "reference", "narration", "journal_id", "status",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)

    def get_allocation_status(self, obj) -> str:
        """Where this receipt's cash ended up - settled, refunded, or still sitting.

        REFUNDED is its own state rather than a flavour of UNALLOCATED: the cash never
        settled a bill, but it is also gone, and calling that "unallocated" is precisely
        what made a refunded receipt keep advertising money that had already been paid
        back out.
        """
        if obj.unallocated_amount <= 0:
            return "ALLOCATED"
        if obj.credit_remaining <= 0:
            return "REFUNDED"
        if obj.allocated_amount <= 0:
            return "UNALLOCATED"
        return "PARTIAL"


class ConcessionSerializer(FieldAccessMixin, IncomeGivenBackMixin, ApprovalGatedMixin, ApprovalStateMixin,
                           serializers.ModelSerializer):
    field_resource = "finance.concession"
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.document_number", read_only=True)
    allowance_account = serializers.CharField(
        source="allowance_account.code", read_only=True, default=None,
    )
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = Concession
        fields = [
            "id", "document_number", "kind", "customer_id", "customer_code",
            "customer_name", "invoice_id", "invoice_number", "concession_date",
            "status", "amount", "amount_naira", "allowance_account",
            "reason", "reference", "approval_required", "income_given_back", "approval_state",
            "approval_returned",
        ]
        list_serializer_class = IncomeGivenBackListSerializer

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


class PaymentPlanInstallmentSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.paymentplan"
    balance = serializers.IntegerField(read_only=True)

    class Meta:
        model = PaymentPlanInstallment
        fields = [
            "id", "seq_no", "due_date", "amount", "amount_settled",
            "balance", "status",
        ]


class PaymentPlanSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """Read shape for a payment plan and its instalments.

    ``branch_id`` is the plan's own branch: its invoice's when it spreads one,
    otherwise its customer's or the one named for a shared customer. An
    instalment payment is deposited only into that branch's bank accounts.
    """

    field_resource = "finance.paymentplan"

    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    invoice_number = serializers.CharField(
        source="invoice.document_number", read_only=True, default=None,
    )
    scheduled_total = serializers.IntegerField(read_only=True)
    settled_total = serializers.IntegerField(read_only=True)
    outstanding_total = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    settled_amount = serializers.IntegerField(source="settled_total", read_only=True)
    installments = PaymentPlanInstallmentSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)

    class Meta:
        model = PaymentPlan
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "branch_id", "branch_name",
            "invoice_id", "invoice_number", "plan_status", "start_date", "frequency",
            "installment_count", "total_amount", "total_naira",
            "baseline_settled", "scheduled_total", "settled_total", "outstanding_total",
            "settled_amount",
            "notes", "installments",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total_amount)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if instance.invoice_id and request is not None and not can_read(
            request, "finance.invoice.settlement"
        ):
            for name in ("baseline_settled", "scheduled_total", "settled_total",
                         "outstanding_total", "settled_amount"):
                data.pop(name, None)
            for row in data.get("installments", []):
                for name in ("amount_settled", "balance", "status"):
                    row.pop(name, None)
        if "settled_total" not in data:
            data.pop("settled_amount", None)
        return data


class DunningStageSerializer(serializers.ModelSerializer):
    class Meta:
        model = DunningStage
        fields = [
            "id", "level", "name", "min_days_overdue", "channel", "message",
        ]


class DunningPolicySerializer(serializers.ModelSerializer):
    stages = DunningStageSerializer(many=True, read_only=True)

    class Meta:
        model = DunningPolicy
        fields = ["id", "name", "is_active", "is_default", "stages"]


class DunningNoticeSerializer(serializers.ModelSerializer):
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.document_number", read_only=True)
    policy_name = serializers.CharField(source="policy.name", read_only=True, default=None)
    amount_due_naira = serializers.SerializerMethodField()

    class Meta:
        model = DunningNotice
        fields = [
            "id", "document_number", "customer_id", "customer_code", "customer_name",
            "invoice_id", "invoice_number", "policy_id", "policy_name", "stage_id",
            "level", "notice_date", "days_overdue", "amount_due", "amount_due_naira",
            "channel", "message", "notice_status", "sent_at",
        ]

    def get_amount_due_naira(self, obj) -> str:
        return format_naira(obj.amount_due)


# --------------------------------------------------------------------------- #
# Setup / reference data                                                      #
# --------------------------------------------------------------------------- #

class CurrencySerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.currency"
    class Meta:
        model = Currency
        fields = ["code", "name", "symbol", "minor_unit", "is_active"]


class FxRateSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.fxrate"
    base = serializers.CharField(source="base_id", read_only=True)
    quote = serializers.CharField(source="quote_id", read_only=True)

    class Meta:
        model = FxRate
        fields = ["id", "base", "quote", "rate", "as_of", "source"]


class TaxCodeSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.taxcode"
    collected_account = serializers.CharField(
        source="collected_account.code", read_only=True, default=None)
    paid_account = serializers.CharField(
        source="paid_account.code", read_only=True, default=None)

    class Meta:
        model = TaxCode
        fields = [
            "id", "code", "name", "rate_bps", "treatment", "is_recoverable",
            "collected_account", "paid_account", "is_active",
        ]


class CostCenterSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.costcenter"
    field_aliases = {"parent_id": "parent_code"}
    parent_code = serializers.CharField(source="parent.code", read_only=True, default=None)

    class Meta:
        model = CostCenter
        fields = ["id", "code", "name", "parent_id", "parent_code", "is_active"]


class DimensionSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.dimension"
    class Meta:
        model = Dimension
        fields = ["id", "code", "name", "allowed_values", "is_active"]


# --------------------------------------------------------------------------- #
# Banking                                                                     #
# --------------------------------------------------------------------------- #

#: The key whose holders read how online payments are settled, branch-bound or not.
PAYMENT_SETTINGS_VIEW = "payments.settings.view"


def _reads_gateway_route(request) -> bool:
    """Whether the caller may read a bank account's provider settlement route.

    A caller who reaches every branch, or who holds ``payments.settings.view``
    (the key that reads the custody setting and each branch's subaccount). A
    branch bursar who keeps Lekki's books without the payments settings key
    sees Lekki's account and balance, not the provider handle money is routed
    by. Answered once per request. A render with no request (a management
    command) passes, as Field Access does.
    """
    if request is None:
        return True
    cached = getattr(request, "_finance_reads_gateway_route", None)
    if cached is None:
        from vs_rbac.permissions import user_has_rbac_permission
        from vs_rbac.scoping import caller_branch_ids

        tenant = getattr(request, "tenant", None) or getattr(request.user, "tenant", None)
        cached = caller_branch_ids(request) is None or user_has_rbac_permission(
            request.user, PAYMENT_SETTINGS_VIEW, tenant=tenant)
        request._finance_reads_gateway_route = cached
    return cached


class BankAccountSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One funding account, under the ``finance.bankaccount`` switches.

    The account's ``name`` identifies it wherever it is picked or printed and
    carries no switch. Everything else about it is registered: the bank and
    its code, the account number, the currency, the ledger account, the
    branch, whether it is active, the main operating account and the fee
    collection account, and the figures worked out from the books (balance,
    reconciliation progress). A caller whose roles cannot read one gets the
    account without it, and the endpoints that write them refuse a role that
    cannot change them.

    ``gateway_subaccount_code``, ``gateway_subaccount_provider`` and
    ``settlement_bank_code`` say how the payment provider settles the branch's
    online payments into this account. Read-only here (the payments
    subaccount endpoint writes them), and present only for a caller who
    reaches the whole tenant or holds ``payments.settings.view``, the readers
    of the custody setting they belong to (:func:`_reads_gateway_route`); for
    anybody else the three keys are absent, not blanked. A caller who passes
    that gate is still filtered by the switches on the subaccount and the
    bank code.
    """

    field_resource = "finance.bankaccount"
    field_access_detail = True

    #: The provider settlement route, shown to :func:`_reads_gateway_route` callers only.
    GATEWAY_ROUTE_FIELDS = (
        "gateway_subaccount_code", "gateway_subaccount_provider", "settlement_bank_code",
    )

    gl_account = serializers.CharField(source="gl_account.code", read_only=True)
    gl_account_name = serializers.CharField(source="gl_account.name", read_only=True)
    currency = serializers.CharField(source="currency_id", read_only=True, default=None)
    book_balance = serializers.SerializerMethodField()
    book_balance_naira = serializers.SerializerMethodField()
    unreconciled_count = serializers.SerializerMethodField()
    last_reconciled_at = serializers.SerializerMethodField()

    class Meta:
        model = BankAccount
        fields = [
            "id", "name", "bank_name", "account_number", "branch_id",
            "gl_account", "gl_account_name", "gl_account_id", "currency",
            "is_active", "is_primary", "is_primary_collection",
            "book_balance", "book_balance_naira", "unreconciled_count",
            "last_reconciled_at",
            "gateway_subaccount_code", "gateway_subaccount_provider", "settlement_bank_code",
        ]
        read_only_fields = [
            "gateway_subaccount_code", "gateway_subaccount_provider", "settlement_bank_code",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not _reads_gateway_route(self.context.get("request")):
            for name in self.GATEWAY_ROUTE_FIELDS:
                data.pop(name, None)
        return data

    def get_book_balance(self, obj):
        from .banking import gl_account_balance
        return gl_account_balance(obj.gl_account)

    def get_book_balance_naira(self, obj):
        from .banking import gl_account_balance
        return format_naira(gl_account_balance(obj.gl_account))

    def get_unreconciled_count(self, obj):
        from .constants import BankLineStatus
        return obj.statement_lines.filter(status=BankLineStatus.UNMATCHED).count()

    def get_last_reconciled_at(self, obj):
        last = obj.reconciliations.order_by("-created_at").values_list(
            "created_at", flat=True).first()
        return last


class BankAccountMetricsSerializer(FieldAccessMixin, serializers.Serializer):
    """The figures at the head of a bank account's detail, under ``finance.bankaccount``.

    The difference between the books and the statement is worked out from both
    balances, so a caller who may read it must be able to read both: with
    either hidden, the other could be recovered from it.
    """

    field_resource = "finance.bankaccount"

    book_balance = serializers.ReadOnlyField()
    statement_balance = serializers.ReadOnlyField()
    unreconciled_diff = serializers.ReadOnlyField()
    unreconciled_count = serializers.ReadOnlyField()

    def to_representation(self, instance):
        from vs_rbac.field_enforcement import can_read

        data = super().to_representation(instance)
        request = self.context.get("request")
        if "unreconciled_diff" in data and not (
            can_read(request, "finance.bankaccount.book_balance")
            and can_read(request, "finance.bankaccount.statement_balance")
        ):
            data.pop("unreconciled_diff")
        return data


class BankAccountActivitySerializer(FieldAccessMixin, serializers.Serializer):
    """One recent ledger line of a bank account, under ``finance.bankaccount``.

    The running balance walks back from the account's book balance, so it is
    read under that switch. The whole list is the account's recent activity
    and is read under its own.
    """

    field_resource = "finance.bankaccount"

    id = serializers.ReadOnlyField()
    date = serializers.ReadOnlyField()
    description = serializers.ReadOnlyField()
    reference = serializers.ReadOnlyField()
    debit = serializers.ReadOnlyField()
    credit = serializers.ReadOnlyField()
    running_balance = serializers.ReadOnlyField()
    matched = serializers.ReadOnlyField()


class BankStatementLineSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.bankaccount"
    field_aliases = {"status": "match_source"}
    amount_naira = serializers.SerializerMethodField()
    match_source_display = serializers.CharField(source="get_match_source_display", read_only=True)
    matched_reference = serializers.SerializerMethodField()
    # Journal lines paired to this statement line via a group (many-to-one) match;
    # empty for a plain 1:1 match (which uses matched_line_id).
    group_line_ids = serializers.SerializerMethodField()
    can_delete = serializers.SerializerMethodField()
    delete_block_reason = serializers.SerializerMethodField()

    class Meta:
        model = BankStatementLine
        fields = [
            "id", "bank_account_id", "statement_id", "txn_date", "description",
            "reference", "amount", "amount_naira", "status", "matched_line_id",
            "group_line_ids", "adjusting_journal_id", "match_source",
            "match_source_display", "matched_reference", "external_id", "reconciled_at",
            "can_delete", "delete_block_reason",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)

    def get_group_line_ids(self, obj) -> list:
        return list(obj.line_matches.values_list("journal_line_id", flat=True))

    def get_matched_reference(self, obj):
        """The document number of the matched journal entry (or adjusting entry)."""
        if obj.adjusting_journal_id:
            return obj.adjusting_journal.document_number
        if obj.matched_line_id and obj.matched_line.entry_id:
            return obj.matched_line.entry.document_number
        return None

    def get_can_delete(self, obj) -> bool:
        from .banking import statement_line_delete_block_reason

        return statement_line_delete_block_reason(obj) is None

    def get_delete_block_reason(self, obj) -> str | None:
        from .banking import statement_line_delete_block_reason

        return statement_line_delete_block_reason(obj)


class BankStatementSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One imported bank statement.

    Each balance has its own switch. The account's statement balance switch
    also gates both figures because they restate the account's balance.
    """

    field_resource = "finance.bankaccount"
    field_aliases = {"opening_balance": "opening_balance_naira"}

    BALANCE_FIELDS = (
        "opening_balance", "opening_balance_naira", "closing_balance", "closing_balance_naira",
    )

    line_count = serializers.IntegerField(read_only=True)
    opening_balance_naira = serializers.SerializerMethodField()
    closing_balance_naira = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    can_edit = serializers.SerializerMethodField()
    edit_block_reason = serializers.SerializerMethodField()
    import_rollback = serializers.SerializerMethodField()

    class Meta:
        model = BankStatement
        fields = [
            "id", "statement_date", "period_label", "opening_balance",
            "opening_balance_naira", "closing_balance", "closing_balance_naira",
            "line_count", "status", "status_display", "can_edit", "edit_block_reason",
            "import_rollback",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request is not None and not can_read(request, "finance.bankaccount.statement_balance"):
            for name in self.BALANCE_FIELDS:
                data.pop(name, None)
        return data

    def get_opening_balance_naira(self, obj) -> str:
        return format_naira(obj.opening_balance)

    def get_closing_balance_naira(self, obj) -> str:
        return format_naira(obj.closing_balance)

    def get_can_edit(self, obj) -> bool:
        from .banking import statement_edit_block_reason

        return statement_edit_block_reason(obj) is None

    def get_edit_block_reason(self, obj) -> str | None:
        from .banking import statement_edit_block_reason

        return statement_edit_block_reason(obj)

    def get_import_rollback(self, obj) -> dict | None:
        from .statement_imports import statement_import_rollback

        return statement_import_rollback(obj)


class BankStatementDetailSerializer(BankStatementSerializer):
    lines = BankStatementLineSerializer(many=True, read_only=True)

    class Meta(BankStatementSerializer.Meta):
        fields = [*BankStatementSerializer.Meta.fields, "lines"]


class BankReconciliationSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One reconciliation run: the books and the statement as they stood.

    A run snapshots the account's book balance and the bank's statement
    balance, so it is read under those two switches of ``finance.bankaccount``.
    The difference is worked out from both, and leaves the run when either is
    hidden.
    """

    field_resource = "finance.bankaccount"

    book_balance_naira = serializers.SerializerMethodField()
    statement_balance_naira = serializers.SerializerMethodField()
    difference_naira = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = BankReconciliation
        fields = [
            "id", "as_of_date", "book_balance", "book_balance_naira",
            "statement_balance", "statement_balance_naira", "difference",
            "difference_naira", "matched_count", "status", "status_display",
            "performed_by_name", "created_at",
        ]

    def to_representation(self, instance):
        from vs_rbac.field_enforcement import can_read

        data = super().to_representation(instance)
        request = self.context.get("request")
        if not (
            can_read(request, "finance.bankaccount.book_balance")
            and can_read(request, "finance.bankaccount.statement_balance")
        ):
            data.pop("difference", None)
            data.pop("difference_naira", None)
        if not can_read(request, "finance.bankaccount.reconciliation_history"):
            data.pop("status", None)
            data.pop("status_display", None)
        return data

    def get_book_balance_naira(self, obj) -> str:
        return format_naira(obj.book_balance)

    def get_statement_balance_naira(self, obj) -> str:
        return format_naira(obj.statement_balance)

    def get_difference_naira(self, obj) -> str:
        return format_naira(obj.difference)

    def get_performed_by_name(self, obj):
        u = obj.performed_by
        if not u:
            return None
        name = " ".join(filter(None, [
            getattr(u, "first_name", ""), getattr(u, "last_name", "")])).strip()
        return name or getattr(u, "email", None)


class BankBookLinesSerializer(FieldAccessMixin, serializers.Serializer):
    """The unmatched ledger lines under the bank account's book-lines switch."""

    field_resource = "finance.bankaccount"
    book_lines = serializers.ListField(child=serializers.DictField(), read_only=True)


class BankAccountDetailSerializer(BankAccountSerializer):
    """A bank account with the blocks its detail screen carries.

    The view attaches ``detail_metrics``, ``detail_transactions``,
    ``detail_statements`` and ``detail_reconciliations`` to the account before
    rendering. Statements and reconciliations are documents of their own and
    are not registered fields of the account.
    """

    metrics = BankAccountMetricsSerializer(source="detail_metrics", read_only=True)
    transactions = BankAccountActivitySerializer(
        source="detail_transactions", many=True, read_only=True)
    statements = BankStatementSerializer(source="detail_statements", many=True, read_only=True)
    reconciliations = BankReconciliationSerializer(
        source="detail_reconciliations", many=True, read_only=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data.get("metrics") == {}:
            data.pop("metrics")
        return data

    class Meta(BankAccountSerializer.Meta):
        fields = BankAccountSerializer.Meta.fields + [
            "metrics", "transactions", "statements", "reconciliations",
        ]


# --------------------------------------------------------------------------- #
# Expense claims                                                              #
# --------------------------------------------------------------------------- #

class ExpenseClaimLineSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.expenseclaim"
    field_aliases = {
        "line_no": "lines", "description": "lines", "expense_account": "lines",
        "quantity": "lines", "tax_code": "lines", "cost_center": "lines",
        "unit_price": "amounts", "net_amount": "amounts",
        "tax_amount": "amounts", "line_total": "amounts",
        "receipt_name": "receipt", "receipt_url": "receipt",
    }
    expense_account = serializers.CharField(source="expense_account.code", read_only=True)
    tax_code = serializers.CharField(source="tax_code.code", read_only=True, default=None)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    line_total = serializers.IntegerField(read_only=True)
    receipt_name = serializers.SerializerMethodField()
    receipt_url = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseClaimLine
        fields = [
            "id", "line_no", "description", "expense_account", "quantity",
            "unit_price", "tax_code", "net_amount", "tax_amount", "line_total",
            "cost_center", "receipt_name", "receipt_url",
        ]

    def get_receipt_name(self, obj):
        if not obj.receipt:
            return None
        return obj.receipt.name.rsplit("/", 1)[-1]

    def get_receipt_url(self, obj):
        if not obj.receipt:
            return None
        request = self.context.get("request")
        return signed_url(obj.receipt.name, absolute_for=request) or None


class ExpenseClaimSerializer(FieldAccessMixin, ApprovalStateMixin, serializers.ModelSerializer):
    """An expense claim with its lines, filtered by the ``finance.expenseclaim`` switches.

    The claimant travels as ``claimant_id`` (the staff member's account, or
    null for a name typed for somebody not on the staff list) and
    ``claimant_name``, and one switch governs both, so a caller who may not
    read the claimant learns neither who it is nor whether they are staff. A
    detail response lists ``claimant`` in ``_read_only_fields`` beside the two
    when the caller may read the claimant and not change it.
    """

    field_resource = "finance.expenseclaim"
    field_access_detail = True
    field_write_aliases = True
    field_aliases = {
        "subtotal": "amounts", "tax_total": "amounts", "total": "amounts",
        "total_naira": "amounts", "amount_paid": "amounts", "balance_due": "amounts",
        "journal_id": "journal",
    }

    lines = ExpenseClaimLineSerializer(many=True, read_only=True)
    balance_due = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    approval_required = serializers.SerializerMethodField()
    reimbursement_account = serializers.CharField(
        source="reimbursement_account.code", read_only=True, default=None,
    )

    class Meta:
        model = ExpenseClaim
        fields = [
            "id", "document_number", "branch_id", "claimant_id", "claimant_name", "claim_date",
            "title", "narration", "status", "payment_status",
            "subtotal", "tax_total", "total", "total_naira",
            "amount_paid", "balance_due", "journal_id", "reimbursement_account",
            "created_at", "approval_required", "lines",
            "approval_state", "approval_returned",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def get_approval_required(self, obj) -> bool:
        from .approvals import ApprovalGate

        gate = self.context.get("approval_gate")
        if gate is None:
            gate = ApprovalGate()
        return gate.required(obj)


# --------------------------------------------------------------------------- #
# Petty cash                                                                  #
# --------------------------------------------------------------------------- #

def _person_name(user):
    """A user's full name, else their email; ``None`` when nobody is named."""
    if user is None:
        return None
    return (getattr(user, "full_name", "") or getattr(user, "email", "") or str(user.pk)).strip()


class PettyCashPeopleListSerializer(serializers.ListSerializer):
    """Resolve whether the people named on a page of petty cash rows have exited, once.

    Each row names its people by the attributes in the child's ``person_fields``,
    so funds and returns share one bulk lookup per response
    (:func:`core.person_exit.prime_exit_states`).
    """

    def to_representation(self, data):
        from core.person_exit import prime_exit_states

        rows = list(data)
        fields = self.child.person_fields
        prime_exit_states(self.context, (
            getattr(row, f"{field}_id") for row in rows for field in fields
        ))
        return super().to_representation(rows)


def _exited(serializer, user_id):
    from core.person_exit import person_is_exited

    return person_is_exited(serializer.context, user_id)


class PettyCashFundSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """A petty cash fund. Each person on it carries a name and an exit flag.

    ``custodian_label`` names the custodian (a user's name, or the free-text
    custodian); ``closed_by_name`` names who closed the fund. Each sits beside
    ``<person>_is_exited``, true when that person has left the tenant's
    employment, and ``None`` when nobody is named.
    """

    field_resource = "finance.pettycash"
    field_access_detail = True
    person_fields = ("custodian", "closed_by")

    gl_account = serializers.CharField(source="gl_account.code", read_only=True)
    custodian_label = serializers.SerializerMethodField()
    custodian_is_exited = serializers.SerializerMethodField()
    closed_by_name = serializers.SerializerMethodField()
    closed_by_is_exited = serializers.SerializerMethodField()
    float_amount_naira = serializers.SerializerMethodField()
    current_balance_naira = serializers.SerializerMethodField()
    shortfall = serializers.IntegerField(read_only=True)

    class Meta:
        model = PettyCashFund
        fields = [
            "id", "name", "branch_id", "gl_account", "gl_account_id",
            "custodian_id", "custodian_name", "custodian_label",
            "float_amount", "float_amount_naira",
            "current_balance", "current_balance_naira", "shortfall",
            "currency", "last_replenished_at", "is_active",
            "state", "closed_on", "closed_by_id",
            "custodian_is_exited", "closed_by_name", "closed_by_is_exited",
        ]
        list_serializer_class = PettyCashPeopleListSerializer

    state = serializers.SerializerMethodField()

    def get_state(self, obj) -> str:
        """``CLOSED`` once the fund's cash has been banked, else ``ACTIVE`` or ``INACTIVE``."""
        if obj.is_closed:
            return "CLOSED"
        return "ACTIVE" if obj.is_active else "INACTIVE"

    def get_custodian_label(self, obj) -> str:
        if obj.custodian_id:
            full = obj.custodian.get_full_name() if hasattr(obj.custodian, "get_full_name") else ""
            return full or getattr(obj.custodian, "email", "") or str(obj.custodian_id)
        return obj.custodian_name

    def get_custodian_is_exited(self, obj):
        return _exited(self, obj.custodian_id)

    def get_closed_by_name(self, obj):
        return _person_name(obj.closed_by) if obj.closed_by_id else None

    def get_closed_by_is_exited(self, obj):
        return _exited(self, obj.closed_by_id)

    def get_float_amount_naira(self, obj) -> str:
        return format_naira(obj.float_amount)

    def get_current_balance_naira(self, obj) -> str:
        return format_naira(obj.current_balance)


class PettyCashVoucherLineSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.pettycashvoucher"
    field_aliases = {"expense_account": "lines"}

    expense_account = serializers.CharField(source="expense_account.code", read_only=True)
    tax_code = serializers.CharField(source="tax_code.code", read_only=True, default=None)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    line_total = serializers.IntegerField(read_only=True)

    class Meta:
        model = PettyCashVoucherLine
        fields = [
            "id", "line_no", "description", "expense_account", "quantity",
            "unit_price", "tax_code", "net_amount", "tax_amount", "line_total",
            "cost_center",
        ]


class PettyCashVoucherSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.pettycashvoucher"
    field_access_detail = True
    lines = PettyCashVoucherLineSerializer(many=True, read_only=True)
    total_naira = serializers.SerializerMethodField()
    expense_account = serializers.SerializerMethodField()

    class Meta:
        model = PettyCashVoucher
        fields = [
            "id", "document_number", "fund_id", "voucher_date", "payee",
            "spent_by_id", "narration", "reference", "status",
            "subtotal", "tax_total", "total", "total_naira",
            "journal_id", "lines", "expense_account",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def get_expense_account(self, obj):
        """The voucher's expense category - the first line's account (code · name)."""
        lines = list(obj.lines.all()[:2])
        if not lines:
            return None
        acc = lines[0].expense_account
        label = f"{acc.code} · {acc.name}"
        return f"{label} +{len(lines) - 1}" if len(lines) > 1 else label


class PettyCashReturnSerializer(FieldAccessMixin, ApprovalStateMixin, serializers.ModelSerializer):
    """A petty cash return: the count, the books, what was banked and the float after.

    Every key but the ids a client links by is a ``finance.pettycash_return`` field: a
    caller who may not read one gets the return without it, and a detail
    response lists in ``_read_only_fields`` the ones they may read and not
    change.

    ``difference`` is the count less the books (positive over, negative short);
    ``cash_left`` is what the tin keeps after the return. ``counted_by_name`` and
    ``created_by_name`` name the people on it, each beside ``<person>_is_exited``.
    ``approval_state`` and ``approval_returned`` say where it stands with its
    approval route (:class:`ApprovalStateMixin`), read once per page: a return
    an approver handed back reads PENDING and returned, and is resumed or its
    request withdrawn, never cancelled underneath the request.
    """

    field_resource = "finance.pettycash_return"
    field_access_detail = True
    field_write_aliases = True

    person_fields = ("counted_by", "created_by")

    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    fund_name = serializers.CharField(source="fund.name", read_only=True)
    bank_account_name = serializers.CharField(
        source="bank_account.name", read_only=True, default=None)
    difference = serializers.IntegerField(read_only=True)
    shortage = serializers.IntegerField(read_only=True)
    overage = serializers.IntegerField(read_only=True)
    cash_left = serializers.IntegerField(read_only=True)
    amount_naira = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    counted_by_name = serializers.SerializerMethodField()
    counted_by_is_exited = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    created_by_is_exited = serializers.SerializerMethodField()

    class Meta:
        model = PettyCashReturn
        list_serializer_class = PettyCashPeopleListSerializer
        fields = [
            "id", "document_number", "status", "kind", "kind_label", "branch_id",
            "fund_id", "fund_name", "bank_account_id", "bank_account_name",
            "return_date", "counted_amount", "book_balance", "difference",
            "shortage", "overage", "difference_reason", "amount", "amount_naira",
            "cash_left", "previous_float_amount", "new_float_amount",
            "counted_by_id", "narration", "reference", "journal_id", "created_by_id",
            "branch_name", "counted_by_name", "counted_by_is_exited",
            "created_by_name", "created_by_is_exited", "approval_state", "approval_returned",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        removed = set()
        if not can_read(request, "finance.pettycash.float_amount"):
            data.pop("previous_float_amount", None)
            removed.add("previous_float_amount")
        if not can_read(request, "finance.pettycash.cash_on_hand"):
            data.pop("book_balance", None)
            removed.add("book_balance")
        if removed and "_read_only_fields" in data:
            data["_read_only_fields"] = [
                name for name in data["_read_only_fields"] if name not in removed
            ]
        return data

    def get_counted_by_name(self, obj):
        return _person_name(obj.counted_by) if obj.counted_by_id else None

    def get_counted_by_is_exited(self, obj):
        return _exited(self, obj.counted_by_id)

    def get_created_by_name(self, obj):
        return _person_name(obj.created_by) if obj.created_by_id else None

    def get_created_by_is_exited(self, obj):
        return _exited(self, obj.created_by_id)


# --------------------------------------------------------------------------- #
# Tax remittance / filing                                                     #
# --------------------------------------------------------------------------- #

class TaxObligationSerializer(serializers.ModelSerializer):
    liability_account = serializers.CharField(source="liability_account.code", read_only=True)
    recoverable_account = serializers.CharField(
        source="recoverable_account.code", read_only=True, default=None,
    )

    class Meta:
        model = TaxObligation
        fields = [
            "id", "code", "name", "obligation_type",
            "liability_account", "liability_account_id",
            "recoverable_account", "recoverable_account_id",
            "authority_name", "frequency", "filing_day", "is_active",
        ]


class TaxFilingShareSerializer(serializers.ModelSerializer):
    """One branch's part of a return: what it declared, what it owes and has paid."""

    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    label = serializers.SerializerMethodField()
    balance_due = serializers.IntegerField(read_only=True)

    class Meta:
        model = TaxFilingShare
        fields = [
            "id", "branch_id", "branch_name", "branch_pending", "label",
            "gross_liability", "recoverable_amount", "brought_forward_credit",
            "adjustment_amount", "amount_due", "amount_paid", "balance_due",
            "carried_forward_credit", "payment_status", "line_count", "filing_journal_id",
        ]

    def get_label(self, obj) -> str:
        if obj.branch_pending:
            return "No branch yet"
        return obj.branch.name if obj.branch_id else "All"


class TaxRemittanceSerializer(serializers.ModelSerializer):
    """One payment of a branch share, and whether it has been reversed."""

    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    bank_account_name = serializers.CharField(source="bank_account.name", read_only=True)
    is_reversed = serializers.BooleanField(read_only=True)

    class Meta:
        model = TaxRemittance
        fields = [
            "id", "share_id", "branch_id", "branch_name", "bank_account_id",
            "bank_account_name", "pay_date", "amount", "journal_id", "is_reversed",
            "reversed_at", "reversal_journal_id", "reversal_reason",
        ]


class TaxFilingSerializer(serializers.ModelSerializer):
    """A return with its per-branch breakdown, late items and remittances.

    ``filing_journal_id`` names the return's one netting/penalty journal: the
    journal of a return filed as a whole, or the single share's journal when the
    return has one share. A return split over several branches lists each
    branch's journal on its share in ``branch_breakdown``.
    """

    obligation_code = serializers.CharField(source="obligation.code", read_only=True)
    obligation_type = serializers.CharField(source="obligation.obligation_type", read_only=True)
    authority_name = serializers.CharField(source="obligation.authority_name", read_only=True)
    liability_account = serializers.CharField(source="obligation.liability_account.code", read_only=True, default=None)
    liability_account_name = serializers.CharField(source="obligation.liability_account.name", read_only=True, default=None)
    balance_due = serializers.IntegerField(read_only=True)
    amount_due_naira = serializers.SerializerMethodField()
    filing_journal_id = serializers.SerializerMethodField()
    branch_breakdown = TaxFilingShareSerializer(source="shares", many=True, read_only=True)
    remittances = TaxRemittanceSerializer(many=True, read_only=True)

    class Meta:
        model = TaxFiling
        fields = [
            "id", "document_number", "obligation_id", "obligation_code",
            "obligation_type", "authority_name", "liability_account", "liability_account_name",
            "period_start", "period_end", "due_date",
            "filing_status", "status",
            "gross_liability", "recoverable_amount", "adjustment_amount",
            "brought_forward_credit", "carried_forward_credit", "credit_from_id",
            "amount_due", "amount_due_naira", "amount_paid", "balance_due",
            "payment_status", "adjustment_account_id",
            "filing_reference", "filed_at", "narration",
            "currency", "filing_journal_id",
            "declared_line_count", "late_line_count", "late_items",
            "branch_breakdown", "remittances",
        ]

    def get_amount_due_naira(self, obj) -> str:
        return format_naira(obj.amount_due)

    def get_filing_journal_id(self, obj):
        if obj.filing_journal_id is not None:
            return obj.filing_journal_id
        shares = list(obj.shares.all())
        return shares[0].filing_journal_id if len(shares) == 1 else None

    def to_representation(self, obj):
        """The return as the reader may see it: whole, or narrowed to their branches.

        ``context["branch_ids"]`` is the reader's branch reach (``None`` for the
        whole tenant). A branch-bound reader is shown only the shares, payments
        and late items of their own branches, and every total on the return is
        the sum of those shares, so nothing another branch declared or paid can
        be read back out of a figure.
        """
        data = super().to_representation(obj)
        reach = self.context.get("branch_ids")
        if reach is None:
            return data
        return _narrow_filing(data, obj, frozenset(reach))


def _narrow_filing(data, obj, reach):
    """``data`` for ``obj`` cut down to the shares of the branches in ``reach``."""
    shares = [s for s in obj.shares.all() if s.branch_id in reach]
    due = sum(s.amount_due for s in shares)
    paid = sum(s.amount_paid for s in shares)
    data.update({
        "branch_breakdown": TaxFilingShareSerializer(shares, many=True).data,
        "remittances": [r for r in data["remittances"] if r["branch_id"] in reach],
        "gross_liability": sum(s.gross_liability for s in shares),
        "recoverable_amount": sum(s.recoverable_amount for s in shares),
        "adjustment_amount": sum(s.adjustment_amount for s in shares),
        "brought_forward_credit": sum(s.brought_forward_credit for s in shares),
        "carried_forward_credit": sum(s.carried_forward_credit for s in shares),
        "amount_due": due, "amount_due_naira": format_naira(due),
        "amount_paid": paid, "balance_due": due - paid,
        "payment_status": (
            "PAID" if due <= 0 or paid >= due else "UNPAID" if paid <= 0 else "PARTIAL"),
        "declared_line_count": sum(s.line_count for s in shares),
        "filing_journal_id": shares[0].filing_journal_id if len(shares) == 1 else None,
    })
    keys = {str(branch_id) for branch_id in reach}
    items = []
    for item in data.get("late_items") or []:
        parts = [part for key, part in (item.get("branches") or {}).items() if key in keys]
        count = sum(part["line_count"] for part in parts)
        if not count:
            continue
        gross = sum(part["gross"] for part in parts)
        recoverable = sum(part["recoverable"] for part in parts)
        items.append({
            "month": item["month"], "label": item["label"], "gross": gross,
            "recoverable": recoverable, "net": gross - recoverable, "line_count": count,
            "branches": {key: part for key, part in item["branches"].items() if key in keys},
        })
    data["late_items"] = items
    data["late_line_count"] = sum(item["line_count"] for item in items)
    return data


# --------------------------------------------------------------------------- #
# Payroll                                                                    #
# --------------------------------------------------------------------------- #

class PayrollLineItemSerializer(serializers.ModelSerializer):
    """One deduction or employer contribution on a line, and the accounts it posted to."""

    class Meta:
        model = PayrollLineItem
        fields = [
            "id", "kind", "code", "label", "amount", "basis_amount", "rate_bps",
            "deduction_type_id", "liability_account_id", "expense_account_id",
        ]


class PayrollLineSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One person's line of a payroll run.

    A line is always a row inside a run, never a detail response of its own,
    so it names no read-only fields: the run that carries it is the record a
    form edits. ``items`` lists each deduction and employer contribution;
    ``tax_basis`` is the PAYE working and ``tax_table_id`` the national table it
    was priced on. Every pay figure, the tax number and the pension PIN sit
    behind their own Field Access switches.
    """

    field_resource = "finance.payrollrun"
    field_aliases = {"employee_is_exited": "employee_name"}

    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    tax_state = serializers.CharField(source="tax_state.code", read_only=True, default=None)
    tax_state_name = serializers.CharField(source="tax_state.name", read_only=True, default=None)
    pfa_name = serializers.CharField(source="pfa.name", read_only=True, default=None)
    paye_source_label = serializers.CharField(source="get_paye_source_display", read_only=True)
    items = PayrollLineItemSerializer(many=True, read_only=True)
    employee_is_exited = serializers.SerializerMethodField()

    class Meta:
        model = PayrollLine
        fields = [
            "id", "line_no", "employee_id", "employee_name", "employee_is_exited", "salary_id",
            "gross_amount", "paye_amount", "pension_amount", "other_deductions_amount",
            "employer_contributions_amount", "net_amount", "taxable_pay",
            "paye_source", "paye_source_label", "tax_table_id", "tax_basis", "items",
            "tax_state", "tax_state_name", "pfa_id", "pfa_name", "tax_id", "pension_pin",
            "components", "cost_center", "branch_id", "branch_name",
            "pay_state", "pay_state_label", "paid_via", "paid_via_label", "pay_note",
            "payout_reference",
        ]

    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    pay_state_label = serializers.CharField(source="get_pay_state_display", read_only=True)
    paid_via_label = serializers.CharField(source="get_paid_via_display", read_only=True)

    def get_employee_is_exited(self, obj):
        from core.person_exit import person_is_exited

        return person_is_exited(self.context, obj.employee_id)

    def to_representation(self, obj):
        data = super().to_representation(obj)
        if obj.employee_id is None:
            data.pop("employee_is_exited", None)
        return data


class PayrollLinePaymentSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One payroll line as the payment progress screen shows it.

    The staff member's name and net pay sit behind the same switches as on the
    run's lines (``finance.payrollrun``), and the payment state, its note and
    the payout reference behind their own: a role that may follow a payroll's
    payment but not read anybody's pay sees how many lines wait for bank
    details and not whose they are or what they earn.
    """

    field_resource = "finance.payrollrun"

    pay_state_label = serializers.CharField(source="get_pay_state_display", read_only=True)
    paid_via_label = serializers.CharField(source="get_paid_via_display", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)

    class Meta:
        model = PayrollLine
        fields = [
            "id", "line_no", "employee_id", "employee_name", "net_amount",
            "branch_id", "branch_name", "pay_state", "pay_state_label", "paid_via",
            "paid_via_label", "pay_note", "payout_reference",
        ]


class StaffBankAccountSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """A staff member's bank account for payroll through the provider, behind Field Access.

    The account number and the holder's name the bank gave are sensitive
    switches of ``finance.staff_bank``; the bank, the verification state and
    who changed and confirmed the account have their own, so a role that follows
    which accounts still need a second person's confirmation need not read
    anybody's account number.

    ``can_confirm`` is the reader's own answer for a screen's Confirm button:
    true only while the account awaits confirmation and the reader holds the
    confirm key and is neither whoever made the last change nor the staff member
    (:func:`vs_finance.staff_banking.reader_may_confirm`). It is derived from the
    verification state, so it travels under that switch and disappears with it.
    """

    field_resource = "finance.staff_bank"
    field_access_detail = True

    employee_name = serializers.SerializerMethodField()
    verification_state_label = serializers.CharField(
        source="get_verification_state_display", read_only=True)
    is_payable = serializers.BooleanField(read_only=True)
    changed_by_name = serializers.SerializerMethodField()
    confirmed_by_name = serializers.SerializerMethodField()
    can_confirm = serializers.SerializerMethodField()

    class Meta:
        model = StaffBankAccount
        fields = [
            "id", "employee_id", "employee_name", "bank_code", "bank_name",
            "account_number", "resolved_account_name", "verification_state",
            "verification_state_label", "verification_note", "verified_at", "is_payable",
            "can_confirm", "changed_by_name", "confirmed_by_name", "confirmed_at", "updated_at",
        ]

    @staticmethod
    def _name(user):
        return (" ".join(p for p in (user.first_name, user.last_name) if p).strip()
                or user.email) if user is not None else None

    def get_employee_name(self, obj):
        return self._name(obj.employee)

    def get_changed_by_name(self, obj):
        return self._name(obj.changed_by)

    def get_confirmed_by_name(self, obj):
        return self._name(obj.confirmed_by)

    def get_can_confirm(self, obj) -> bool:
        from .staff_banking import reader_may_confirm

        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None:
            return False
        tenant = (getattr(request, "rbac_tenant", None) or getattr(request, "tenant", None)
                  or getattr(user, "tenant", None))
        return reader_may_confirm(obj, user, tenant)


class StaffWithoutBankAccountSerializer(FieldAccessMixin, serializers.Serializer):
    """A payroll roster person who has no staff bank account yet, for choosing whose to add.

    Renders an annotated roster row (:func:`vs_finance.views_ops.payroll._salary_rows`).
    ``employee_id`` is the person's user id, the key the account is added under
    (``POST /v1/finance/staff-bank-accounts/<employee_id>/``). The name is the
    roster's, and the branch is the one the row belongs to on the reader's today
    (:meth:`~vs_finance.models.EmployeeSalaryQuerySet.with_branch_on`). The person
    follows the ``finance.staff_bank`` staff member switch. It is not a
    ``ModelSerializer``, so the roster's own serializers are not read as surfaces
    of the staff bank resource.
    """

    field_resource = "finance.staff_bank"

    employee_id = serializers.IntegerField(read_only=True)
    employee_name = serializers.CharField(source="name", read_only=True)
    branch_id = serializers.IntegerField(source="branch_on_id", read_only=True, allow_null=True)
    branch_name = serializers.CharField(source="branch_on_name", read_only=True, allow_null=True)


class PayrollRunPeopleListSerializer(serializers.ListSerializer):
    """Read employment flags for a page of runs with one grouped lookup."""

    def to_representation(self, data):
        from core.person_exit import prime_exit_states

        runs = list(data)
        prime_exit_states(self.context, (
            line.employee_id for run in runs for line in run.lines.all()
        ))
        return super().to_representation(runs)


class PayrollRunBranchSerializer(serializers.ModelSerializer):
    """One branch's share of a run posted one journal per branch, and how it was paid."""

    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model = PayrollRunBranch
        fields = [
            "id", "branch_id", "branch_name", "status",
            "gross_total", "paye_total", "pension_total", "other_deductions_total",
            "employer_contributions_total", "net_total",
            "journal_id", "disbursement_journal_id", "bank_account_id",
        ]


class PayrollRunSerializer(FieldAccessMixin, ApprovalStateMixin, serializers.ModelSerializer):
    """A payroll run, whole or as the reader's branches' part of it.

    ``branch_shares`` is empty unless the run posted one journal per branch,
    when it lists each branch's figures, journals and payment.

    A central run (``branch_id`` null) covers every branch's staff. A
    branch-bound reader reaches one only through their own branch's share, and
    is shown that part alone: their branches' lines and shares, totals summed
    from those lines, and a status that follows their shares, so Lekki's bursar
    reads Lekki's January as paid once Lekki's staff are paid. Nothing another
    branch is paid can be read back out of a figure. ``partial_view`` says the
    response is such a part. The reader's reach is ``context["branch_ids"]``
    (``None`` for the whole tenant); without it, the reach of
    ``context["request"]``'s caller, so a response built without either key
    is narrowed rather than whole.
    """

    field_resource = "finance.payrollrun"

    lines = PayrollLineSerializer(many=True, read_only=True)
    branch_shares = PayrollRunBranchSerializer(many=True, read_only=True)
    net_total_naira = serializers.SerializerMethodField()
    partial_view = serializers.SerializerMethodField()
    # Which branch the run covers; null is a central run over every branch.
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    # Statutory liability accounts the run credited (set on post) - let the FE match the
    # real outstanding balance (trial balance) to show remittance status honestly.
    paye_payable_account = serializers.CharField(source="paye_payable_account.code", read_only=True, default=None)
    pension_payable_account = serializers.CharField(source="pension_payable_account.code", read_only=True, default=None)

    class Meta:
        model = PayrollRun
        list_serializer_class = PayrollRunPeopleListSerializer
        fields = [
            "id", "document_number", "pay_date", "period_label", "narration",
            "run_status", "status", "gross_total", "paye_total", "pension_total",
            "other_deductions_total", "employer_contributions_total",
            "net_total", "net_total_naira", "bank_account_id",
            "branch_id", "branch_name",
            "paye_payable_account", "paye_payable_account_id",
            "pension_payable_account", "pension_payable_account_id",
            "journal_id", "disbursement_journal_id", "branch_shares", "lines",
            "partial_view", "approval_state", "approval_returned",
        ]

    def get_net_total_naira(self, obj) -> str:
        return format_naira(obj.net_total)

    def get_partial_view(self, obj) -> bool:
        return self._part_reach(obj) is not None

    def _part_reach(self, obj):
        """The reader's branch ids when ``obj`` is a central run shown in part, else None."""
        if obj.branch_id is not None:
            return None
        if "branch_ids" in self.context:
            reach = self.context["branch_ids"]
        else:
            from vs_rbac.scoping import caller_branch_ids

            request = self.context.get("request")
            reach = caller_branch_ids(request) if request is not None else None
        return None if reach is None else frozenset(reach)

    def to_representation(self, obj):
        from core.person_exit import prime_exit_states

        prime_exit_states(self.context, (line.employee_id for line in obj.lines.all()))
        data = super().to_representation(obj)
        reach = self._part_reach(obj)
        if reach is None:
            return data
        return _narrow_run(data, obj, reach)


def _narrow_run(data, obj, reach):
    """``data`` for the central run ``obj`` cut down to the branches in ``reach``."""
    from .constants import PayrollRunStatus

    lines = [line for line in obj.lines.all() if line.branch_id in reach]
    kept = {line.pk for line in lines}
    shares = [share for share in obj.branch_shares.all() if share.branch_id in reach]
    net = sum(line.net_amount for line in lines)
    data.update({
        "lines": [row for row in data["lines"] if row["id"] in kept],
        "branch_shares": [row for row in data["branch_shares"] if row["branch_id"] in reach],
        "gross_total": sum(line.gross_amount for line in lines),
        "paye_total": sum(line.paye_amount for line in lines),
        "pension_total": sum(line.pension_amount for line in lines),
        "other_deductions_total": sum(line.other_deductions_amount for line in lines),
        "employer_contributions_total": sum(
            line.employer_contributions_amount for line in lines),
        "net_total": net,
        "net_total_naira": format_naira(net),
    })
    statuses = {share.status for share in shares}
    if statuses:
        data["run_status"] = next(
            (only for only in (PayrollRunStatus.PAID, PayrollRunStatus.CANCELLED)
             if statuses == {only}),
            PayrollRunStatus.POSTED,
        )
    return data


class SalaryComponentSerializer(serializers.ModelSerializer):
    """A structure line. Not a registered field - a structure is configuration
    (e.g. 'Basic = 40% of gross'), not any one person's pay."""

    class Meta:
        model = SalaryComponent
        fields = [
            "id", "name", "kind", "calc_method", "rate_bps", "amount",
            "is_basic", "is_pensionable", "is_taxable", "statutory_type", "sequence",
            "effective_from", "effective_to",
        ]


class SalaryStructureSerializer(serializers.ModelSerializer):
    components = SalaryComponentSerializer(many=True, read_only=True)
    employee_count = serializers.SerializerMethodField()

    class Meta:
        model = SalaryStructure
        fields = [
            "id", "name", "description", "is_active", "components", "employee_count",
        ]

    def get_employee_count(self, obj) -> int:
        # annotated by the list view; fall back to a count for the detail view.
        cached = getattr(obj, "employee_count_annot", None)
        return cached if cached is not None else obj.employee_salaries.count()


def _derived_pay(gross, structure, paye, pension, as_at) -> dict:
    """PAYE, pension, net and the breakdown of one set of terms, as the roster shows them.

    Derived from the structure's lines in force on ``as_at`` when the terms
    name one, else the typed figures.
    """
    from .payroll import apply_structure

    if structure is not None:
        return apply_structure(gross, structure, as_at)
    return {"paye": paye, "pension": pension, "net": gross - paye - pension, "components": []}


class EmployeeSalaryNextTermsSerializer(FieldAccessMixin, serializers.Serializer):
    """The next dated change to a roster row's pay terms, its pay figures behind their switches.

    Rendered from a mapping by :class:`EmployeeSalarySerializer`. A nested
    serializer carrying the mixin, so a caller who may not read a person's pay
    learns that a change is coming and from when, never what it is.
    """

    field_resource = "finance.salary"

    effective_from = serializers.DateField(read_only=True)
    branch_id = serializers.IntegerField(read_only=True, allow_null=True)
    branch_name = serializers.CharField(read_only=True, allow_null=True)
    gross_amount = serializers.IntegerField(read_only=True)
    paye_amount = serializers.IntegerField(read_only=True)
    pension_amount = serializers.IntegerField(read_only=True)


class EmployeeSalarySerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One roster row, with the pay figures behind their own switches.

    Which pay figures a person may see is ``finance.salary``'s question, and
    which roster rows they may reach at all is the roster endpoints'. The two
    are separate on purpose: an officer assigning branches needs the roster
    without needing anybody's salary.

    The pay terms (branch, structure, gross, PAYE, pension and the breakdown,
    cost centre, state of residence) are the ones in force on the reader's
    today (:meth:`~vs_finance.models.EmployeeSalary.terms_shown_on`), never the
    row's own columns, which mirror the latest version even when it starts next
    year. Aisha is on N300,000 with a raise to N320,000 from January 2027: the
    roster shows N300,000 until January, and ``next_terms`` carries the raise
    and its date. ``terms_effective_from`` is when the terms shown took effect
    (null for a row with no history), and is later than today for a hire who
    has not started. The day comes from the context's ``today``, which the
    roster views set to the reader's own; without it, the tenant's today.
    Rows are expected with their ``versions`` prefetched, with each version's
    structure lines, so a page costs no query per row.
    """

    field_resource = "finance.salary"
    field_access_detail = True
    field_write_aliases = True

    cost_center = serializers.SerializerMethodField()
    structure_id = serializers.SerializerMethodField()
    structure_name = serializers.SerializerMethodField()
    gross_amount = serializers.SerializerMethodField()
    terms_effective_from = serializers.SerializerMethodField()
    next_terms = serializers.SerializerMethodField()
    # Not a registered field: which site somebody works at is not a pay figure, and it has
    # to be readable by whoever is assigning branches before a school can switch to
    # per-branch payroll. ``branch_name`` is null for an unassigned row, which is
    # the state the frontend filters on to find who is still blocking the switch.
    # Where the roster annotated it, the branch owning the row today, so a move
    # dated from next month shows the branch still paying the person.
    branch_id = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    # The account this row is for, where one is known. Not a registered field: WHO a
    # roster row is about is not a pay figure, and it is what lets a caller ask
    # whether the person being paid still works here. Null on every row written
    # before the link existed, and there is no backfill, so a null means "not
    # linked yet" rather than "not an employee".
    employee_id = serializers.IntegerField(read_only=True, allow_null=True)
    # Where PAYE goes and where pension goes. Not pay figures; the tax number,
    # PIN, rent and any PAYE override are, and sit behind their own switches.
    residence_state = serializers.SerializerMethodField()
    residence_state_name = serializers.SerializerMethodField()
    pfa_id = serializers.IntegerField(read_only=True, allow_null=True)
    pfa_name = serializers.CharField(source="pfa.name", read_only=True, default=None)
    # The figures a tenant that supplies its own PAYE is paid on: derived from the
    # structure's lines in force today when one is assigned, else the typed
    # figures. A tenant whose PAYE is computed has PAYE and pension worked out on
    # each run. Computed once per row (memoised) to avoid re-walking the components.
    paye_amount = serializers.SerializerMethodField()
    pension_amount = serializers.SerializerMethodField()
    net_amount = serializers.SerializerMethodField()
    components = serializers.SerializerMethodField()

    class Meta:
        model = EmployeeSalary
        fields = [
            "id", "name", "employee_id", "branch_id", "branch_name",
            "structure_id", "structure_name",
            "gross_amount", "paye_amount", "pension_amount", "net_amount", "components",
            "cost_center", "is_active",
            "residence_state", "residence_state_name", "tax_id", "pfa_id", "pfa_name",
            "pension_pin", "annual_rent", "paye_override", "paye_override_reason",
            "terms_effective_from", "next_terms",
        ]

    def _today(self, obj):
        today = self.context.get("today")
        if today is None:
            from vs_config.clock import tenant_today

            today = tenant_today(obj.entity.tenant)
        return today

    def _terms(self, obj):
        """The terms shown on the reader's today, memoised on the row."""
        cache = getattr(obj, "_shown_terms_cache", None)
        if cache is None:
            cache = obj.terms_shown_on(self._today(obj))
            obj._shown_terms_cache = cache
        return cache

    def _derived(self, obj) -> dict:
        cache = getattr(obj, "_derived_cache", None)
        if cache is None:
            terms = self._terms(obj)
            cache = _derived_pay(
                terms.gross_amount, terms.structure, terms.paye_amount,
                terms.pension_amount, self._today(obj),
            )
            obj._derived_cache = cache
        return cache

    def get_branch_id(self, obj):
        return getattr(obj, "branch_on_id", self._terms(obj).branch_id)

    def get_branch_name(self, obj):
        if hasattr(obj, "branch_on_name"):
            return obj.branch_on_name
        terms = self._terms(obj)
        return terms.branch.name if terms.branch_id else None

    def get_structure_id(self, obj):
        return self._terms(obj).structure_id

    def get_structure_name(self, obj):
        terms = self._terms(obj)
        return terms.structure.name if terms.structure_id else None

    def get_cost_center(self, obj):
        terms = self._terms(obj)
        return terms.cost_center.code if terms.cost_center_id else None

    def get_residence_state(self, obj):
        terms = self._terms(obj)
        return terms.residence_state.code if terms.residence_state_id else None

    def get_residence_state_name(self, obj):
        terms = self._terms(obj)
        return terms.residence_state.name if terms.residence_state_id else None

    def get_gross_amount(self, obj) -> int:
        return self._terms(obj).gross_amount

    def get_terms_effective_from(self, obj):
        terms = self._terms(obj)
        return terms.effective_from.isoformat() if terms is not obj else None

    def get_next_terms(self, obj):
        change = obj.next_terms_after(self._today(obj))
        if change is None:
            return None
        derived = _derived_pay(
            change.gross_amount, change.structure, change.paye_amount,
            change.pension_amount, change.effective_from,
        )
        return EmployeeSalaryNextTermsSerializer({
            "effective_from": change.effective_from,
            "branch_id": change.branch_id,
            "branch_name": change.branch.name if change.branch_id else None,
            "gross_amount": change.gross_amount,
            "paye_amount": derived["paye"],
            "pension_amount": derived["pension"],
        }, context=self.context).data

    def get_paye_amount(self, obj) -> int:
        return self._derived(obj)["paye"]

    def get_pension_amount(self, obj) -> int:
        return self._derived(obj)["pension"]

    def get_net_amount(self, obj) -> int:
        return self._derived(obj)["net"]

    def get_components(self, obj) -> list:
        return self._derived(obj)["components"]


# --------------------------------------------------------------------------- #
# Statutory payroll                                                           #
# --------------------------------------------------------------------------- #

class EmployeeSalaryVersionSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One dated version of a person's pay terms, with the pay figures behind their switches."""

    field_resource = "finance.salary"

    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    structure_name = serializers.CharField(source="structure.name", read_only=True, default=None)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    residence_state = serializers.CharField(source="residence_state.code", read_only=True, default=None)
    created_by = serializers.CharField(source="created_by.email", read_only=True, default=None)

    class Meta:
        model = EmployeeSalaryVersion
        fields = [
            "id", "effective_from", "branch_id", "branch_name", "structure_id",
            "structure_name", "gross_amount", "paye_amount", "pension_amount",
            "cost_center", "residence_state", "reason", "created_by", "created_at",
        ]


class PayeTaxBandSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayeTaxBand
        fields = ["id", "sequence", "lower", "upper", "rate_bps"]


class PayeTaxReliefSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayeTaxRelief
        fields = [
            "id", "sequence", "code", "name", "kind", "basis", "rate_bps",
            "cap_amount", "floor_amount",
        ]


class PayeTaxTableSerializer(serializers.ModelSerializer):
    """A national tax table: public law, readable by anyone who runs payroll."""

    bands = PayeTaxBandSerializer(many=True, read_only=True)
    reliefs = PayeTaxReliefSerializer(many=True, read_only=True)

    class Meta:
        model = PayeTaxTable
        fields = [
            "id", "country", "tax_year", "name", "source_reference", "notes",
            "minimum_tax_rate_bps", "exempt_income_threshold", "revision", "is_active",
            "bands", "reliefs", "updated_at",
        ]


class PayrollTaxJurisdictionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayrollTaxJurisdiction
        fields = ["id", "country", "code", "name", "authority_name", "is_active"]


class PensionFundAdministratorSerializer(serializers.ModelSerializer):
    class Meta:
        model = PensionFundAdministrator
        fields = ["id", "code", "name", "is_active"]


class PayrollDeductionTypeSerializer(serializers.ModelSerializer):
    liability_account = serializers.CharField(source="liability_account.code", read_only=True)

    class Meta:
        model = PayrollDeductionType
        fields = ["id", "code", "name", "liability_account", "liability_account_id", "is_active"]


class EmployeeDeductionSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One person's voluntary deduction; its amount and limit are their pay breakdown.

    Which deduction a person has is not a pay figure. How much it takes from
    their pay is, and travels behind ``finance.salary``'s pay breakdown switch,
    as the same deduction does on their payroll line.
    """

    field_resource = "finance.salary"

    deduction_type_code = serializers.CharField(source="deduction_type.code", read_only=True)
    deduction_type_name = serializers.CharField(source="deduction_type.name", read_only=True)

    class Meta:
        model = EmployeeDeduction
        fields = [
            "id", "salary_id", "deduction_type_id", "deduction_type_code",
            "deduction_type_name", "amount", "start_date", "end_date", "total_limit",
            "reference", "is_active",
        ]


class PayBroughtForwardSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """A person's pay brought forward into a tax year, figures behind their switches.

    ``source`` says whose months they are: a previous employer's, or this
    employer's own from before its payroll ran here. Each figure travels under
    the ``finance.salary`` switch of the same figure of the person's own pay
    (:mod:`vs_finance.field_access`); whose months they are, the employer's
    name and where the figures come from are not pay figures.
    """

    field_resource = "finance.salary"
    field_access_detail = True

    brought_forward_gross_amount = serializers.IntegerField(source="gross_amount", read_only=True)
    brought_forward_taxable_pay = serializers.IntegerField(source="taxable_pay", read_only=True)
    brought_forward_paye_amount = serializers.IntegerField(source="paye_amount", read_only=True)
    brought_forward_pension_amount = serializers.IntegerField(source="pension_amount", read_only=True)
    brought_forward_nhf_amount = serializers.IntegerField(source="nhf_amount", read_only=True)
    created_by = serializers.CharField(source="created_by.email", read_only=True, default=None)
    updated_by = serializers.CharField(source="updated_by.email", read_only=True, default=None)

    class Meta:
        model = PayBroughtForward
        fields = [
            "id", "salary_id", "tax_year", "source", "employer_name", "evidence_reference",
            "brought_forward_gross_amount", "brought_forward_taxable_pay", "brought_forward_paye_amount",
            "brought_forward_pension_amount", "brought_forward_nhf_amount",
            "created_by", "updated_by", "created_at", "updated_at",
        ]


class PayslipSerializer(serializers.ModelSerializer):
    """A payslip in a list of the reader's own: what it is for and what it paid them.

    Only ever rendered for the employee the payslip is about, so the figures are
    theirs to read.
    """

    entity = serializers.CharField(source="entity.code", read_only=True)
    run = serializers.CharField(source="run.document_number", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    gross_amount = serializers.IntegerField(source="line.gross_amount", read_only=True)
    net_amount = serializers.IntegerField(source="line.net_amount", read_only=True)
    paye_amount = serializers.IntegerField(source="line.paye_amount", read_only=True)

    class Meta:
        model = Payslip
        fields = [
            "id", "entity", "run", "pay_date", "period_label", "branch_name",
            "gross_amount", "paye_amount", "net_amount", "issued_at", "email_status",
        ]


# --------------------------------------------------------------------------- #
# Budgets                                                                     #
# --------------------------------------------------------------------------- #

class BudgetLineSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "finance.budget"
    account = serializers.CharField(source="account.code", read_only=True)
    cost_center = serializers.CharField(source="cost_center.code", read_only=True, default=None)

    class Meta:
        model = BudgetLine
        fields = ["id", "account", "account_id", "cost_center", "period_no", "amount"]


class BudgetSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """A budget, with the branch it belongs to and whether the reader may change it.

    ``branch_id`` is empty only on a budget raised before every budget named a
    branch, which only a whole-school reader reaches. ``can_manage`` answers for
    the request's reader (see :func:`vs_rbac.scoping.caller_may_change`).
    """

    field_resource = "finance.budget"
    field_access_detail = True

    fiscal_year = serializers.IntegerField(source="fiscal_year.year", read_only=True)
    is_locked = serializers.BooleanField(read_only=True)
    lines = BudgetLineSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = Budget
        fields = [
            "id", "code", "name", "fiscal_year", "fiscal_year_id", "status",
            "is_locked", "approved_at", "lines", "branch_id", "branch_name", "can_manage",
        ]

    def get_can_manage(self, obj) -> bool:
        from vs_rbac.scoping import caller_may_change, visible_branch_ids

        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None:
            return True
        # One scope lookup per response, not per row of a list.
        if "_visible_branches" not in self.context:
            self.context["_visible_branches"] = visible_branch_ids(user, obj.entity.tenant)
        return caller_may_change(
            user, obj.entity.tenant, [obj.branch_id], visible=self.context["_visible_branches"],
        )


# --------------------------------------------------------------------------- #
# Fixed assets                                                                #
# --------------------------------------------------------------------------- #

class DepreciationScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = DepreciationSchedule
        fields = ["id", "seq", "depreciation_date", "amount", "is_posted",
                  "journal_id", "posted_at"]


class FixedAssetSerializer(serializers.ModelSerializer):
    schedule = DepreciationScheduleSerializer(many=True, read_only=True)
    net_book_value = serializers.IntegerField(read_only=True)
    depreciable_base = serializers.IntegerField(read_only=True)
    cost_naira = serializers.SerializerMethodField()
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    method_display = serializers.CharField(source="get_method_display", read_only=True)

    class Meta:
        model = FixedAsset
        fields = [
            "id", "document_number", "branch_id", "name", "asset_code", "category", "category_display",
            "acquisition_date", "cost", "cost_naira", "salvage_value", "useful_life_months",
            "method", "method_display", "asset_status", "status", "accumulated_depreciation", "net_book_value",
            "depreciable_base", "acquisition_journal_id", "disposal_date",
            "disposal_journal_id", "schedule",
        ]

    def get_cost_naira(self, obj) -> str:
        return format_naira(obj.cost)


# --------------------------------------------------------------------------- #
# Audit log                                                                   #
# --------------------------------------------------------------------------- #

#: Audit targets whose snapshots carry registered fields, and the Field Access
#: resource those fields are registered under. The receivables policy's
#: snapshots are keyed by the policy's own setting names, which are the client
#: names ``finance.settings`` registers.
#: Audit actions whose snapshots carry registered fields whatever target they
#: name: every finance settings section is a ``finance.settings`` field, and
#: the record retention change names the books themselves as its target.
from .constants import FinanceAuditAction  # noqa: E402 - constants import no models

AUDIT_SNAPSHOT_FIELDS_BY_ACTION = {
    FinanceAuditAction.FINANCE_SETTINGS_UPDATED: "finance.settings",
    FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED: "finance.settings",
    FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED: "finance.settings",
    FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED: "finance.settings",
    FinanceAuditAction.FINANCE_RECEIVABLES_SETTINGS_UPDATED: "finance.settings",
    FinanceAuditAction.PAYROLL_SETTINGS_UPDATED: "finance.settings",
    FinanceAuditAction.RETENTION_SETTINGS_UPDATED: "finance.settings",
}

AUDIT_SNAPSHOT_FIELDS = {
    "Budget": "finance.budget",
    "EmployeeSalary": "finance.salary",
    "FinanceReceivablesPolicy": "finance.settings",
    "FinanceBankingSettings": "finance.settings",
    "Customer": "finance.customer",
    "Invoice": "finance.invoice",
    "CreditNote": "finance.creditnote",
    "Refund": "finance.refund",
    "Concession": "finance.concession",
    "WriteOff": "finance.writeoff",
    "CreditTransfer": "finance.credittransfer",
    "PaymentPlan": "finance.paymentplan",
    "JournalEntry": "finance.journal",
    "Account": "finance.account",
    "CostCenter": "finance.costcenter",
    "BankAccount": "finance.bankaccount",
    "BankTransaction": "finance.banktransaction",
    "BankTransfer": "finance.banktransfer",
    "DeferredIncomeRelease": "finance.deferredincome",
    "DepositForfeiture": "finance.deposit",
    "DoubtfulDebtProvision": "finance.provision",
    "PettyCashReturn": "finance.pettycash_return",
    "InterBranchTransfer": "finance.interbranch",
    "HeldForBranchReceipt": "finance.interbranch",
    "InterBranchRecharge": "finance.interbranch",
    "PayerPayment": "finance.payer_payment",
    "PayrollRun": "finance.payrollrun",
    "FiscalYear": "finance.fiscalyear",
    "ProcurementSettings": "procurement.settings",
}


#: Audit targets whose snapshots carry a figure a registered field guards under
#: another name: target type -> (Field Access key, snapshot keys it governs). A
#: bank statement's balances are the bank's own, read under the account's
#: ``statement_balance`` switch.
AUDIT_SNAPSHOT_GUARDS = {
    "BankStatement": (
        "finance.bankaccount.statement_balance", ("opening_balance", "closing_balance"),
    ),
}


class FinanceAuditPeopleListSerializer(serializers.ListSerializer):
    """Resolve the named actors for one finance trail page in bulk."""

    def to_representation(self, data):
        from core.person_exit import prime_exit_states

        rows = list(data)
        prime_exit_states(self.context, (
            user_id for row in rows
            for user_id in (row.actor_id, row.effective_user_id)
        ))
        return super().to_representation(rows)


class FinanceAuditLogSerializer(serializers.ModelSerializer):
    """One row of the finance trail.

    ``before``/``after`` are the field-level snapshot the UI summarises ("N
    fields changed"); ``metadata`` is an internal bag (ids, request context)
    with no reader value and is not exposed.

    ``actor`` is the email of the person who really acted, and ``actor_name``
    their name, which is what a reader is shown. An action taken under
    a proxy also names whom they acted as: ``real_actor_name``,
    ``proxied_user_name`` and the ready ``acted_label`` ("Ada Obi for Chioma
    Okafor") come from :mod:`core.attribution`. Querysets feeding this
    serializer select ``actor``, ``effective_user`` and ``branch`` together.

    ``branch_id``/``branch_name`` are the branch of the document the entry is
    about, null for an entry about the whole tenant or written before entries
    carried a branch. One share of a central payroll run or of the tenant's tax
    return is its own entry, so a whole-school reader tells the shares apart by
    this column.

    An entry about a roster row snapshots that person's pay, tax ID and pension
    PIN under the row's own field names, so ``before``/``after`` are filtered
    through the reader's Field Access on ``finance.salary``
    (:data:`AUDIT_SNAPSHOT_FIELDS`): an auditor with pay figures switched off
    reads that Ada was added and by whom, and none of her pay. A render with no
    request in its context keeps the snapshot whole.

    ``message`` says what happened to which document and where, and carries no
    amount, balance, address, narration or reason: those are the snapshot's, so
    Field Access reaches them. An entry written before sentences were
    figure-free shows the sentence rebuilt from its action and document number
    (:func:`vs_finance.audit.entry_sentence`); its stored text is never served.

    ``reason`` is the free-text reason the person gave for the act (cancelling
    an order, terminating a contract, declining a request), served beside the
    sentence and never inside it (:func:`vs_finance.audit.entry_reason`). It is
    empty when there is none, and when it belongs to a field the caller cannot
    read.
    """

    actor = serializers.CharField(source="actor.email", read_only=True, default=None)
    actor_name = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    real_actor_name = serializers.SerializerMethodField()
    proxied_user_name = serializers.SerializerMethodField()
    acted_label = serializers.SerializerMethodField()
    actor_is_exited = serializers.SerializerMethodField()
    effective_user_is_exited = serializers.SerializerMethodField()
    reason = serializers.CharField(read_only=True, default="")

    class Meta:
        model = FinanceAuditLog
        list_serializer_class = FinanceAuditPeopleListSerializer
        fields = [
            "id", "action", "action_display", "status", "actor", "actor_name", "target_type",
            "target_id", "document_number", "message", "reason", "before", "after", "created_at",
            "real_actor_name", "proxied_user_name", "acted_label", "branch_id", "branch_name",
            "actor_is_exited", "effective_user_is_exited",
        ]

    def get_actor_is_exited(self, obj):
        from core.person_exit import person_is_exited

        return person_is_exited(self.context, obj.actor_id)

    def get_actor_name(self, obj):
        from core.attribution import person_name

        return person_name(obj.actor) if obj.actor_id else None

    def get_effective_user_is_exited(self, obj):
        from core.person_exit import person_is_exited

        return person_is_exited(self.context, obj.effective_user_id)

    def _attribution(self, obj) -> dict:
        from core.attribution import audit_row_attribution

        return audit_row_attribution(obj)

    def to_representation(self, obj):
        from .audit import entry_reason, entry_sentence, mirror_summary

        data = super().to_representation(obj)
        data["message"] = entry_sentence(obj)
        request = self.context.get("request")
        if obj.action in (FinanceAuditAction.BUDGET_APPROVED,
                          FinanceAuditAction.BUDGET_DELETED) and not can_read(
                              request, "finance.budget.name"):
            data["message"] = mirror_summary(
                obj.action, obj.document_number or obj.target_id,
            )
        resource = (AUDIT_SNAPSHOT_FIELDS.get(obj.target_type)
                    or AUDIT_SNAPSHOT_FIELDS_BY_ACTION.get(obj.action))
        if resource is not None:
            data["before"] = visible(request, resource, data.get("before") or {})
            data["after"] = visible(request, resource, data.get("after") or {})
        guard = AUDIT_SNAPSHOT_GUARDS.get(obj.target_type)
        if guard is not None and not can_read(request, guard[0]):
            for name in ("before", "after"):
                if isinstance(data.get(name), dict):
                    data[name] = {k: v for k, v in data[name].items() if k not in guard[1]}
        data["reason"] = entry_reason(
            obj, can_read=lambda key: can_read(request, key),
            snapshots={"before": data.get("before"), "after": data.get("after")},
        )
        return data

    def get_real_actor_name(self, obj) -> str | None:
        return self._attribution(obj)["real_actor_name"]

    def get_proxied_user_name(self, obj) -> str | None:
        return self._attribution(obj)["proxied_user_name"]

    def get_acted_label(self, obj) -> str:
        return self._attribution(obj)["acted_label"]


class SettingsHistorySerializer(FinanceAuditLogSerializer):
    """A settings change, with each touched setting labelled and its values in words.

    ``changes`` is built from the ``before`` and ``after`` this row would show
    anyway, after any Field Access filtering, so it can never reveal more than
    they do. A bank summary in a snapshot (the collection account) loses the
    bank and currency of a bank account the caller may not read. The context carries ``fields`` (the family's
    :class:`~vs_finance.settings_history.SettingField` map), ``tenant`` for date
    formats and ``accounts`` (code to name) for account mappings; see
    :func:`vs_finance.settings_history.settings_history`.
    """

    def to_representation(self, obj):
        from .settings_history import describe_changes

        from .settings_history import hide_bank_summary_copies

        data = super().to_representation(obj)
        request = self.context.get("request")
        data["before"] = hide_bank_summary_copies(request, data.get("before"))
        data["after"] = hide_bank_summary_copies(request, data.get("after"))
        data["changes"] = describe_changes(
            self.context.get("fields") or {}, data.get("before"), data.get("after"),
            tenant=self.context.get("tenant"), accounts=self.context.get("accounts"),
        )
        return data


# --------------------------------------------------------------------------- #
# Customer document email deliveries                                          #
# --------------------------------------------------------------------------- #

class FinanceDeliveryPeopleListSerializer(serializers.ListSerializer):
    """Resolve delivery requesters once for a history list."""

    def to_representation(self, data):
        from core.person_exit import prime_exit_states

        rows = list(data)
        prime_exit_states(self.context, (row.requested_by_id for row in rows))
        return super().to_representation(rows)


class FinanceDocumentDeliverySerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One attempt to email a customer document.

    ``recipients`` and ``bcc`` are the addresses the send actually used, which is the
    point of the history: a user asking "did this reach them" needs the address, not
    just a status. They are the customer's own billing address and the finance CC, so
    nothing here discloses a third party. ``notification_ids`` stays internal - it is a
    correlation handle for support, with no reader value.

    ``recipients`` is the customer's billing email as it stood when the send was made,
    so it is read under the billing email switch of ``finance.customer``: a caller who
    may not read that field sees that a send happened, to how many addresses, and not
    which. An address the product hosting the books added, such as a pupil's primary
    guardian, is further read under its own key, and shown as who the contact is to a
    caller who may not read it. ``bcc`` is the finance monitoring copy and belongs to the school, not to the
    customer.
    """

    field_resource = "finance.customer"

    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_code = serializers.CharField(source="customer.code", read_only=True)
    document_type_display = serializers.CharField(source="get_document_type_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    requested_by_is_exited = serializers.SerializerMethodField()
    recipient_count = serializers.SerializerMethodField()
    can_retry = serializers.SerializerMethodField()

    class Meta:
        model = FinanceDocumentDelivery
        list_serializer_class = FinanceDeliveryPeopleListSerializer
        fields = [
            "id", "customer_id", "customer_code", "customer_name",
            "document_type", "document_type_display", "document_id", "document_number",
            "period_start", "period_end", "source", "source_display", "status",
            "requested_by_name", "requested_by_is_exited",
            "recipients", "recipient_count", "bcc", "note",
            "queued_at", "sent_at", "failure_reason", "can_retry", "created_at",
        ]

    def get_requested_by_name(self, obj) -> str:
        user = obj.requested_by
        if user is None:
            # Automatic sends have no actor. Saying so beats an empty cell that
            # reads as missing data.
            return "System"
        return (getattr(user, "full_name", "") or getattr(user, "email", "")).strip()

    def get_requested_by_is_exited(self, obj):
        from core.person_exit import person_is_exited

        return person_is_exited(self.context, obj.requested_by_id)

    def get_recipient_count(self, obj) -> int:
        return len(obj.recipients or [])

    def get_can_retry(self, obj) -> bool:
        from .constants import FinanceDeliveryStatus

        return obj.status == FinanceDeliveryStatus.FAILED

    def to_representation(self, instance):
        """A guardian's address the caller may not read is shown as who they are."""
        from .field_guards import shown_recipients

        data = super().to_representation(instance)
        if "recipients" in data:
            data["recipients"] = shown_recipients(
                self.context.get("request"), data["recipients"], instance.recipient_contacts,
            )
        return data


class DocumentEmailPreviewSerializer(FieldAccessMixin, serializers.Serializer):
    """What a send of a customer document would do, under ``finance.customer``.

    ``recipients`` is the customer's billing email, so a caller who may not read it
    is told whether a send is possible and not where it would go. A guardian or
    other contact the product named is shown as who they are to a caller who may
    not read their address (:func:`vs_finance.field_guards.shown_recipients`).
    """

    field_resource = "finance.customer"

    recipients = serializers.ReadOnlyField()
    bcc = serializers.ReadOnlyField()
    subject = serializers.ReadOnlyField()
    can_send = serializers.ReadOnlyField()
    blocked_reason = serializers.ReadOnlyField()
    deliveries = FinanceDocumentDeliverySerializer(many=True, read_only=True)

    def to_representation(self, instance):
        """A guardian's address the caller may not read is shown as who they are."""
        from .field_guards import shown_recipients

        data = super().to_representation(instance)
        if "recipients" in data:
            data["recipients"] = shown_recipients(
                self.context.get("request"), data["recipients"], instance.get("recipient_contacts"),
            )
        return data


class DoubtfulDebtProvisionLineSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """One branch's figures in a provision run, under the ``finance.provision`` switches."""

    field_resource = "finance.provision"

    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)

    class Meta:
        model = DoubtfulDebtProvisionLine
        fields = [
            "branch_id", "branch_name", "required", "current", "movement", "bands",
            "journal_id",
        ]


class DoubtfulDebtProvisionSerializer(FieldAccessMixin, ApprovalGatedMixin, ApprovalStateMixin,
                                      serializers.ModelSerializer):
    """A doubtful-debt provision run, whole or as the reader's branches' part of it.

    A run is raised for every branch at once and names no branch, so a
    branch-bound reader reaches one only through a line for a branch they work
    in, and is shown that part alone: their branches' lines (with each line's
    age bands and journal) and ``required_total`` and ``movement_total`` summed
    from those lines. Lekki's bursar reads the N1,800 the run provides at Lekki
    and nothing of Ikeja's line, figures or journal. ``partial_view`` says the
    response is such a part. ``approval_required`` is null in a part: whether a
    run needs approval can turn on its whole total, so a yes or no would tell
    Lekki's bursar something about the other branches' figures.

    The reader's reach is ``context["branch_ids"]`` (``None`` for the whole
    tenant). Without that key the run is shown whole, which is right only for
    the writes, every one of which a whole-tenant caller makes; every read
    passes the key.

    The run and its lines are the ``finance.provision`` resource. A branch
    part is cut by each line's ``branch_id``, which is always present. The
    part's totals replace the run's only when the payload carries them: a total
    the caller may not read stays out.

    The policy snapshot is the provision bands the run was worked out with, and
    each line's age bands are keyed by the bands' thresholds with the amount
    each provides, which gives the rate away. Both are the receivables policy's
    ``finance.settings.provision_bands`` seen through a run, so they are served
    only to a caller who may read that setting.
    """

    field_resource = "finance.provision"
    field_access_detail = True

    lines = DoubtfulDebtProvisionLineSerializer(many=True, read_only=True)
    partial_view = serializers.SerializerMethodField()

    class Meta:
        model = DoubtfulDebtProvision
        fields = [
            "id", "document_number", "status", "as_of", "narration", "required_total",
            "movement_total", "policy_snapshot", "lines", "approval_required",
            "created_at", "partial_view", "approval_state", "approval_returned",
        ]

    def _part_reach(self):
        reach = self.context.get("branch_ids")
        return None if reach is None else frozenset(reach)

    def get_partial_view(self, obj) -> bool:
        return self._part_reach() is not None

    def get_approval_required(self, obj):
        if self._part_reach() is not None:
            return None
        return super().get_approval_required(obj)

    def to_representation(self, obj):
        data = super().to_representation(obj)
        if not can_read(self.context.get("request"), "finance.settings.provision_bands"):
            data.pop("policy_snapshot", None)
            for row in data.get("lines", []):
                row.pop("bands", None)
        reach = self._part_reach()
        if reach is not None:
            lines = [line for line in obj.lines.all() if line.branch_id in reach]
            data["lines"] = [row for row in data["lines"] if row["branch_id"] in reach]
            if "required_total" in data:
                data["required_total"] = sum(int(line.required) for line in lines)
            if "movement_total" in data:
                data["movement_total"] = sum(abs(int(line.movement)) for line in lines)
        return data


class CustomerDepositSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """A customer's refundable deposit and where it stands.

    Every field is a ``finance.deposit`` field. A deposit is opened by billing
    and closed by the release and forfeit runs, so none is writable.
    """

    field_resource = "finance.deposit"

    customer_code = serializers.CharField(source="customer.code", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.document_number", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    release_note_number = serializers.CharField(
        source="release_note.document_number", read_only=True, default=None)
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = CustomerDeposit
        fields = [
            "id", "customer_id", "customer_code", "customer_name", "branch_id",
            "branch_name", "invoice_id", "invoice_number", "amount", "amount_naira",
            "status", "claim_opened_on", "release_note_number", "forfeiture_id",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)
