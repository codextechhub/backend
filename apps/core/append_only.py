"""The tables whose rows are never changed once written, and the triggers that hold them.

An audit trail that Python promises not to change is only as strong as the
next queryset ``update()``, data migration or support engineer's shell. Each
table here carries a BEFORE UPDATE and a BEFORE DELETE trigger installed by
its own app's migration, which refuses the write at the database whatever
issued it (a narrow, documented exception is named in each installing
migration).

Two rules keep them standing, and ``core.tests_append_only`` enforces both:

* every trigger listed here exists and is enabled on its table, so a later
  migration that drops one fails the suite rather than silently ending the
  guarantee;
* no migration names one of these triggers in a ``DROP TRIGGER`` or disables
  triggers on one of these tables, except the migration that installs it (whose
  reverse removes it), listed in :data:`INSTALLING_MIGRATIONS`.

A migration also never deletes audit rows. With the triggers in place such a
migration fails when it runs, which is the intended outcome: retiring audit
history is a decision for a person, not a deploy.
"""

#: table -> (update trigger, delete trigger)
APPEND_ONLY_TRIGGERS = {
    "vs_finance_financeauditlog": (
        "vs_finance_financeauditlog_no_update", "vs_finance_financeauditlog_no_delete",
    ),
    "vs_finance_ledgerseal": ("vs_finance_ledgerseal_no_update", "vs_finance_ledgerseal_no_delete"),
    "vs_audit_auditevent": ("vs_audit_auditevent_no_update", "vs_audit_auditevent_no_delete"),
    "vs_payments_paymentevent": (
        "vs_payments_paymentevent_no_update", "vs_payments_paymentevent_no_delete",
    ),
    "vs_workflow_workflowauditlog": (
        "vs_workflow_workflowauditlog_no_update", "vs_workflow_workflowauditlog_no_delete",
    ),
    "vs_config_configurationauditevent": ("vs_config_audit_no_update", "vs_config_audit_no_delete"),
}

#: ``(app label, migration name without its number)`` of each migration allowed to
#: drop one of these triggers: the ones that install or narrow them. Matched
#: without the number so renumbering a migration does not break the rule.
INSTALLING_MIGRATIONS = {
    ("vs_finance", "financeauditlog_immutability_triggers"),
    ("vs_finance", "finance_audit_branch"),
    ("vs_finance", "record_retention_seals_and_archive"),
    ("vs_audit", "audit_trail_append_only"),
    ("vs_payments", "payment_events_append_only"),
    ("vs_workflow", "workflow_audit_append_only"),
    ("vs_config", "configuration_audit_immutability"),
}
