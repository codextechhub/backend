from __future__ import annotations


class ImportPermission:
    """
    Canonical RBAC permission keys for the import data app.
    Pattern: import.<resource>.<action>
    """

    # ── Templates ─────────────────────────────────────────────────────────────
    TEMPLATE_VIEW     = "import.templates.view"
    TEMPLATE_CREATE   = "import.templates.create"
    TEMPLATE_MANAGE   = "import.templates.manage"   # internal config, platform staff

    # ── Batches ───────────────────────────────────────────────────────────────
    BATCH_VIEW        = "import.batches.view"
    BATCH_CREATE      = "import.batches.create"
    BATCH_UPDATE      = "import.batches.update"
    BATCH_DELETE      = "import.batches.delete"
    BATCH_VALIDATE    = "import.batches.run"        # trigger validation / re-validate
    BATCH_IMPORT      = "import.batches.import"     # trigger actual import execution

    # ── Validation issues ─────────────────────────────────────────────────────
    VALIDATION_VIEW   = "import.validations.view"
    VALIDATION_RESOLVE = "import.validations.update"

    # ── Jobs ──────────────────────────────────────────────────────────────────
    JOB_VIEW          = "import.jobs.view"

    # ── Rollbacks ─────────────────────────────────────────────────────────────
    ROLLBACK_VIEW     = "import.rollbacks.view"
    ROLLBACK_RUN      = "import.rollbacks.run"

    # ── Audit / notifications ─────────────────────────────────────────────────
    AUDIT_VIEW        = "import.audit.view"
    NOTIFICATION_VIEW = "import.notifications.view"


#: How many reversible rows a rollback may hold before it leaves the request.
#:
#: Reversing a row is not uniform work. A user or a branch row is one locked
#: read and one delete, but a school row takes a census of every reverse
#: relation on its tenant and then an ordered teardown across ten apps, so a
#: file of fifty schools is thousands of queries with an operator watching a
#: spinner. Past this many rows the rollback is queued and the result is read
#: from the rollback history instead.
#:
#: Below it, the rollback stays in the request on purpose: the per-row outcomes
#: are the point of the response, and for a handful of rows the operator should
#: see immediately which ones refused and why.
ROLLBACK_INLINE_ROW_LIMIT = 50
