"""Bank-statement integration for the shared import wizard.

The generic wizard owns files, lifecycle, issue reporting and jobs. This module
owns finance-specific normalization and the statement-wide invariants that must
pass before publish creates any reconciliation rows.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .constants import BankLineStatus
from .money import to_kobo, to_naira


def _column_resolver(template):
    mapping = {column.target_field: column.column_name for column in template.columns.all()}
    return lambda target: mapping.get(target, target)


def _clean_text(value) -> str:
    return str(value or "").strip()


def _major_amount(value, *, allow_blank=True) -> int | None:
    """Parse a human major-unit amount into exact minor units."""
    text = _clean_text(value)
    if not text:
        return None if allow_blank else 0
    text = text.replace(",", "").replace("₦", "").strip()
    if text.upper().startswith("NGN"):
        text = text[3:].strip()
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("Expected a valid currency amount.")
    if not amount.is_finite():
        raise ValueError("Expected a finite currency amount.")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Amounts may contain at most two decimal places.")
    return to_kobo(amount)


def _existing_external_ids(bank_account, external_ids) -> set[str]:
    from .models import BankStatementLine

    found: set[str] = set()
    values = sorted(set(external_ids))
    for offset in range(0, len(values), 500):
        found.update(
            BankStatementLine.objects.filter(
                bank_account=bank_account,
                external_id__in=values[offset:offset + 500],
            ).values_list("external_id", flat=True)
        )
    return found


def _existing_fingerprints(bank_account, dates) -> set[tuple]:
    from .models import BankStatementLine

    found: set[tuple] = set()
    values = sorted(set(dates))
    for offset in range(0, len(values), 500):
        found.update(
            BankStatementLine.objects.filter(
                bank_account=bank_account,
                txn_date__in=values[offset:offset + 500],
            ).values_list("txn_date", "amount", "description", "reference")
        )
    return found


def validate_bank_statement_import_batch(import_batch) -> dict:
    """Return finance-specific wizard issues and a footing summary."""
    from .models import BankStatementImportContext

    issues: list[dict] = []
    try:
        context = import_batch.bank_statement_context
    except BankStatementImportContext.DoesNotExist:
        return {
            "issues": [{
                "severity": "error",
                "code": "business_rule",
                "message": "This bank-statement import has no finance context.",
            }],
            "summary": {},
        }

    if not import_batch.template:
        return {"issues": issues, "summary": {}}

    if context.bank_account.entity.tenant_id != import_batch.tenant_id:
        issues.append({
            "severity": "error",
            "code": "business_rule",
            "message": "The selected bank account does not belong to this import tenant.",
        })

    duplicate_file = BankStatementImportContext.objects.filter(
        bank_account=context.bank_account,
        source_file_hash=context.source_file_hash,
        published_statement__isnull=False,
    ).exclude(pk=context.pk).exists()
    if duplicate_file:
        issues.append({
            "severity": "error",
            "code": "duplicate_record",
            "message": "This exact statement file has already been published for this bank account.",
        })

    rows = import_batch.preview_rows or []
    col = _column_resolver(import_batch.template)
    normalized: list[dict] = []
    external_id_rows: dict[str, list[int]] = {}
    fingerprint_rows: dict[tuple, list[int]] = {}
    previous_date = None
    balance_presence: list[bool] = []

    total_in = 0
    total_out = 0

    for row_number, row in enumerate(rows, start=1):
        date_value = _clean_text(row.get(col("txn_date")))
        try:
            txn_date = datetime.date.fromisoformat(date_value)
        except ValueError:
            # The generic template validator already emits the detailed date issue.
            continue

        if txn_date > context.statement_date:
            issues.append({
                "severity": "error",
                "code": "business_rule",
                "message": "Transaction date cannot be after the statement closing date.",
                "row_number": row_number,
                "column_name": col("txn_date"),
                "raw_value": date_value,
            })
        if previous_date and txn_date < previous_date:
            issues.append({
                "severity": "error",
                "code": "business_rule",
                "message": "Transactions must be ordered oldest to newest.",
                "row_number": row_number,
                "column_name": col("txn_date"),
                "raw_value": date_value,
            })
        previous_date = txn_date

        amounts = {}
        amount_error = False
        for target in ("money_in", "money_out"):
            raw = row.get(col(target))
            try:
                amounts[target] = _major_amount(raw) or 0
            except ValueError as exc:
                amount_error = True
                issues.append({
                    "severity": "error",
                    "code": "invalid_format",
                    "message": str(exc),
                    "row_number": row_number,
                    "column_name": col(target),
                    "raw_value": raw,
                })
        if amount_error:
            continue

        money_in = amounts["money_in"]
        money_out = amounts["money_out"]
        if money_in < 0 or money_out < 0:
            issues.append({
                "severity": "error",
                "code": "business_rule",
                "message": "Money In and Money Out cannot be negative.",
                "row_number": row_number,
            })
            continue
        if (money_in > 0) == (money_out > 0):
            issues.append({
                "severity": "error",
                "code": "business_rule",
                "message": "Enter an amount in exactly one of Money In or Money Out.",
                "row_number": row_number,
            })
            continue

        amount = money_in - money_out
        total_in += money_in
        total_out += money_out
        description = _clean_text(row.get(col("description")))
        reference = _clean_text(row.get(col("reference")))
        external_id = _clean_text(row.get(col("external_id")))
        raw_balance = row.get(col("balance"))
        balance_presence.append(bool(_clean_text(raw_balance)))

        parsed_balance = None
        if _clean_text(raw_balance):
            try:
                parsed_balance = _major_amount(raw_balance, allow_blank=False)
            except ValueError as exc:
                issues.append({
                    "severity": "error",
                    "code": "invalid_format",
                    "message": str(exc),
                    "row_number": row_number,
                    "column_name": col("balance"),
                    "raw_value": raw_balance,
                })

        item = {
            "row_number": row_number,
            "txn_date": txn_date,
            "amount": amount,
            "description": description,
            "reference": reference,
            "external_id": external_id,
            "balance": parsed_balance,
        }
        normalized.append(item)
        if external_id:
            external_id_rows.setdefault(external_id, []).append(row_number)
        else:
            fingerprint = (txn_date, amount, description, reference)
            fingerprint_rows.setdefault(fingerprint, []).append(row_number)

    for external_id, row_numbers in external_id_rows.items():
        if len(row_numbers) > 1:
            for row_number in row_numbers:
                issues.append({
                    "severity": "error",
                    "code": "duplicate_record",
                    "message": f"Transaction ID '{external_id}' appears more than once in this file.",
                    "row_number": row_number,
                    "column_name": col("external_id"),
                    "raw_value": external_id,
                })

    existing_ids = _existing_external_ids(context.bank_account, external_id_rows)
    for external_id in sorted(existing_ids):
        issues.append({
            "severity": "error",
            "code": "duplicate_record",
            "message": f"Transaction ID '{external_id}' has already been imported.",
            "row_number": external_id_rows[external_id][0],
            "column_name": col("external_id"),
            "raw_value": external_id,
        })

    existing_fingerprints = _existing_fingerprints(
        context.bank_account,
        [fingerprint[0] for fingerprint in fingerprint_rows],
    )
    for fingerprint, row_numbers in fingerprint_rows.items():
        if fingerprint in existing_fingerprints:
            issues.append({
                "severity": "error",
                "code": "duplicate_record",
                "message": "This transaction matches a statement line already imported.",
                "row_number": row_numbers[0],
            })
        if len(row_numbers) > 1:
            for row_number in row_numbers[1:]:
                issues.append({
                    "severity": "warning",
                    "code": "duplicate_record",
                    "message": (
                        "This row is identical to another row in the file. "
                        "It will be imported because banks can report genuine repeated transactions."
                    ),
                    "row_number": row_number,
                })

    movement = total_in - total_out
    computed_closing = context.opening_balance + movement
    if computed_closing != context.closing_balance:
        issues.append({
            "severity": "error",
            "code": "business_rule",
            "message": (
                "Statement does not balance: opening balance plus net movement "
                "does not equal the reported closing balance."
            ),
            "metadata": {
                "opening_balance": context.opening_balance,
                "net_movement": movement,
                "computed_closing_balance": computed_closing,
                "reported_closing_balance": context.closing_balance,
            },
        })

    if any(balance_presence) and not all(balance_presence):
        issues.append({
            "severity": "error",
            "code": "business_rule",
            "message": "Either provide Balance for every transaction row or leave the entire column blank.",
            "column_name": col("balance"),
        })
    elif balance_presence and all(balance_presence):
        running = context.opening_balance
        for item in normalized:
            running += item["amount"]
            if item["balance"] is not None and item["balance"] != running:
                issues.append({
                    "severity": "error",
                    "code": "business_rule",
                    "message": "Running balance does not agree with the transaction movement.",
                    "row_number": item["row_number"],
                    "column_name": col("balance"),
                    "metadata": {
                        "computed_balance": running,
                        "reported_balance": item["balance"],
                    },
                })

    return {
        "issues": issues,
        "summary": {
            "bank_account_id": context.bank_account_id,
            "statement_date": context.statement_date.isoformat(),
            "row_count": len(rows),
            "money_in": total_in,
            "money_out": total_out,
            "net_movement": movement,
            "opening_balance": context.opening_balance,
            "computed_closing_balance": computed_closing,
            "reported_closing_balance": context.closing_balance,
            "opening_balance_major": str(to_naira(context.opening_balance)),
            "computed_closing_balance_major": str(to_naira(computed_closing)),
            "reported_closing_balance_major": str(to_naira(context.closing_balance)),
        },
    }


def normalized_statement_rows(import_batch) -> list[dict]:
    """Map validated template rows to the finance import service contract."""
    col = _column_resolver(import_batch.template)
    rows = []
    for row in import_batch.preview_rows or []:
        money_in = _major_amount(row.get(col("money_in"))) or 0
        money_out = _major_amount(row.get(col("money_out"))) or 0
        rows.append({
            "txn_date": datetime.date.fromisoformat(_clean_text(row.get(col("txn_date")))),
            "amount": money_in - money_out,
            "description": _clean_text(row.get(col("description"))),
            "reference": _clean_text(row.get(col("reference"))),
            "external_id": _clean_text(row.get(col("external_id"))),
        })
    return rows


def execute_bank_statement_import(import_batch, queued_by):
    """Publish one fully validated statement as a single atomic wizard job."""
    from vs_import_data.models import (
        ImportBatchStatusChoices,
        ImportJobRowResult,
        ImportJobStatusChoices,
        ImportRowActionChoices,
    )
    from vs_import_data.services.import_executor import (
        finalize_import_job,
        start_import_job,
    )
    from .banking import import_statement_lines
    from .models import BankAccount, BankStatementImportContext

    job = start_import_job(import_batch=import_batch, queued_by=queued_by)
    try:
        with transaction.atomic():
            context = (
                BankStatementImportContext.objects
                .select_for_update()
                .get(import_batch=import_batch)
            )
            # Serialize publish for one account so two simultaneously validated files
            # cannot both pass duplicate detection and then create the same lines.
            context.bank_account = BankAccount.objects.select_for_update().get(
                pk=context.bank_account_id,
            )
            if context.published_statement_id:
                raise ValueError("This import batch has already been published.")

            validation = validate_bank_statement_import_batch(import_batch)
            blocking = [
                issue for issue in validation["issues"]
                if issue.get("severity", "error") == "error"
            ]
            if blocking:
                raise ValueError(
                    "Bank statement changed or now conflicts with existing data; "
                    "re-upload and validate it before publishing."
                )

            rows = normalized_statement_rows(import_batch)
            statement, created, suspected = import_statement_lines(
                context.bank_account,
                rows,
                statement_date=context.statement_date,
                period_label=context.period_label,
                opening_balance=context.opening_balance,
                closing_balance=context.closing_balance,
                actor_user=queued_by,
            )
            if statement is None or suspected or len(created) != len(rows):
                raise ValueError(
                    "A duplicate was detected while publishing. No statement lines were imported."
                )

            context.published_statement = statement
            context.save(update_fields=["published_statement", "updated_at"])

            ImportJobRowResult.objects.bulk_create([
                ImportJobRowResult(
                    job=job,
                    row_number=index,
                    action=ImportRowActionChoices.CREATE,
                    target_model="BankStatementLine",
                    target_object_pk=str(line.pk),
                    status_message="Bank statement line imported.",
                    row_payload=raw,
                    normalized_payload={
                        **normalized,
                        "txn_date": normalized["txn_date"].isoformat(),
                    },
                )
                for index, (raw, normalized, line) in enumerate(
                    zip(import_batch.preview_rows or [], rows, created),
                    start=1,
                )
            ])

            # Commit the imported finance rows and the wizard's success state
            # together. If finalization/auditing fails, the statement is rolled
            # back and the exception path records one failed job.
            processed = len(rows)
            job.processed_rows = processed
            job.succeeded_rows = processed
            job.failed_rows = 0
            job.skipped_rows = 0
            job.progress_percent = 100
            job.save(update_fields=[
                "processed_rows", "succeeded_rows", "failed_rows", "skipped_rows",
                "progress_percent", "updated_at",
            ])
            finalize_import_job(
                import_batch=import_batch,
                job=job,
                processed_rows=processed,
                succeeded_rows=processed,
                failed_rows=0,
                skipped_rows=0,
            )
            job.execution_summary = {
                **job.execution_summary,
                "statement_id": statement.pk,
                "bank_account_id": context.bank_account_id,
            }
            job.save(update_fields=["execution_summary", "updated_at"])
        return job
    except Exception as exc:
        job.status = ImportJobStatusChoices.FAILED
        job.completed_at = timezone.now()
        job.last_error_code = "BANK_STATEMENT_PUBLISH_FAILED"
        job.last_error_message = str(exc)
        job.save(update_fields=[
            "status", "completed_at", "last_error_code", "last_error_message", "updated_at",
        ])
        import_batch.status = ImportBatchStatusChoices.IMPORT_FAILED
        import_batch.is_ready_for_import = False
        import_batch.save(update_fields=["status", "is_ready_for_import", "updated_at"])
        raise


@transaction.atomic
def rollback_bank_statement_import_job(job, *, initiated_by=None, reason=""):
    """Delete an imported statement only while none of its lines were acted upon."""
    from vs_import_data.models import (
        ImportBatchStatusChoices,
        ImportJobStatusChoices,
        ImportRollbackRecord,
    )
    from vs_import_data.services.audit_service import create_import_audit_log
    from .models import BankStatementImportContext

    context = (
        BankStatementImportContext.objects
        .select_for_update()
        .get(import_batch=job.import_batch)
    )
    statement = context.published_statement
    if statement is None:
        raise ValueError("This bank-statement import has no published statement to roll back.")

    acted_on = statement.lines.filter(
        Q(status__in=[BankLineStatus.MATCHED, BankLineStatus.IGNORED])
        | Q(matched_line__isnull=False)
        | Q(adjusting_journal__isnull=False)
        | Q(line_matches__isnull=False)
    ).exists()
    if acted_on:
        raise ValueError(
            "This statement cannot be rolled back after any line is matched, ignored or adjusted."
        )

    line_count = statement.lines.count()
    statement.lines.all().delete()
    statement.delete()
    context.published_statement = None
    context.save(update_fields=["published_statement", "updated_at"])

    now = timezone.now()
    record = ImportRollbackRecord.objects.create(
        job=job,
        initiated_by=initiated_by,
        reason=reason,
        was_successful=True,
        reverted_rows_count=line_count,
        started_at=now,
        completed_at=now,
        details={"statement_lines_deleted": line_count},
    )
    job.status = ImportJobStatusChoices.ROLLED_BACK
    job.rollback_started_at = now
    job.rollback_completed_at = now
    job.save(update_fields=[
        "status", "rollback_started_at", "rollback_completed_at", "updated_at",
    ])
    batch = job.import_batch
    batch.status = ImportBatchStatusChoices.ROLLED_BACK
    batch.save(update_fields=["status", "updated_at"])

    create_import_audit_log(
        school=batch.school,
        branch=batch.branch,
        actor=initiated_by,
        import_batch=batch,
        job=job,
        action="import_rollback",
        entity_type="BankStatement",
        entity_id="",
        before_data={"status": "published"},
        after_data={"status": "rolled_back"},
        message="Bank statement import rolled back.",
        metadata={"reason": reason, "statement_lines_deleted": line_count},
    )
    return record
