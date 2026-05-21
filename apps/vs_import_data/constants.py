from __future__ import annotations


class ImportPermission:
    """
    Canonical RBAC permission keys for the import data app.
    Pattern: import.<resource>.<action>
    """

    # ── Templates ─────────────────────────────────────────────────────────────
    TEMPLATE_VIEW     = "import.templates.view"
    TEMPLATE_CREATE   = "import.templates.create"
    TEMPLATE_MANAGE   = "import.templates.manage"   # internal config, CX_STAFF

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

    # ── Row corrections ───────────────────────────────────────────────────────
    CORRECTION_VIEW   = "import.corrections.view"
    CORRECTION_CREATE = "import.corrections.create"

    # ── Jobs ──────────────────────────────────────────────────────────────────
    JOB_VIEW          = "import.jobs.view"

    # ── Rollbacks ─────────────────────────────────────────────────────────────
    ROLLBACK_VIEW     = "import.rollbacks.view"
    ROLLBACK_RUN      = "import.rollbacks.run"

    # ── Audit / notifications ─────────────────────────────────────────────────
    AUDIT_VIEW        = "import.audit.view"
    NOTIFICATION_VIEW = "import.notifications.view"
