# =============================================================================
# vs_notifications / constants.py
#
# All enums, TextChoices, and the EVENT_TYPE_REGISTRY that seeds the
# NotificationEventType table.  Adding a new event type means:
#   1. Add an entry to EVENT_TYPE_REGISTRY below.
#   2. Run: python manage.py seed_notification_event_types
#   3. Run: python manage.py seed_notification_templates   (creates default body)
# =============================================================================


# ---------------------------------------------------------------------------
# Channel choices
# ---------------------------------------------------------------------------

# Persisted delivery channel values used by settings, templates, and notifications.
class ChannelChoices:
    IN_APP = "in_app"
    EMAIL  = "email"

    CHOICES = [
        (IN_APP, "In-App"),
        (EMAIL,  "Email"),
    ]

    ALL = [IN_APP, EMAIL]


# ---------------------------------------------------------------------------
# Notification status choices
# ---------------------------------------------------------------------------

# Delivery lifecycle values for individual Notification rows.
class NotificationStatus:
    PENDING = "PENDING"
    SENT    = "SENT"
    FAILED  = "FAILED"

    CHOICES = [
        (PENDING, "Pending"),
        (SENT,    "Sent"),
        (FAILED,  "Failed"),
    ]

    # Terminal states — no further transitions allowed
    TERMINAL = {SENT, FAILED}


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

# Stable API/service error codes returned by notification workflows.
class NotificationErrorCode:
    UNKNOWN_EVENT_TYPE                    = "UNKNOWN_EVENT_TYPE"
    UNKNOWN_CHANNEL                       = "UNKNOWN_CHANNEL"
    UNSUPPORTED_CHANNEL                   = "UNSUPPORTED_CHANNEL"
    DUPLICATE_TEMPLATE                    = "DUPLICATE_TEMPLATE"
    INVALID_TEMPLATE_SYNTAX               = "INVALID_TEMPLATE_SYNTAX"
    READ_STATE_NOT_SUPPORTED_FOR_CHANNEL  = "READ_STATE_NOT_SUPPORTED_FOR_CHANNEL"
    IN_APP_ALWAYS_ENABLED                 = "IN_APP_ALWAYS_ENABLED"
    TRANSACTIONAL_NOT_CONFIGURABLE        = "TRANSACTIONAL_NOT_CONFIGURABLE"
    FILTER_REQUIRED                       = "FILTER_REQUIRED"
    ACCESS_DENIED                         = "ACCESS_DENIED"
    NO_EMAIL_ADDRESS                      = "NO_EMAIL_ADDRESS"


# ---------------------------------------------------------------------------
# Config flag keys (read via vs_config FlagService at runtime)
# ---------------------------------------------------------------------------

# Runtime tuning keys for Celery email delivery behavior.
class NotificationConfigKey:
    EMAIL_MAX_RETRIES       = "notification_email_max_retries"
    EMAIL_RETRY_BACKOFF_SEC = "notification_email_retry_backoff_seconds"

    DEFAULTS = {
        EMAIL_MAX_RETRIES:       3,
        EMAIL_RETRY_BACKOFF_SEC: 60,
    }


# ---------------------------------------------------------------------------
# Permission keys
# (must match entries in the vs_rbac seed — communication.* namespace)
# ---------------------------------------------------------------------------

# RBAC keys that protect notification administration and history endpoints.
class NotificationPermission:
    TEMPLATE_CONFIGURE        = "communication.notification_templates.configure"
    BULK_SEND                 = "communication.bulk_notifications.send"
    EMAIL_SEND                = "communication.email_notifications.send"
    TRACK_DELIVERY            = "communication.message_delivery.track"
    VIEW_HISTORY              = "communication.message_history.view"
    FILTER_MESSAGES           = "communication.messages_by_type.filter"
    ENFORCE_PERMISSIONS       = "communication.communication_permissions.enforce"
    LOG_EVENTS                = "communication.communication_events.log"
    AUDIT_ACTIVITY            = "communication.message_activity.audit"


# ---------------------------------------------------------------------------
# Event type registry
#
# Each entry defines one NotificationEventType row.
# Fields:
#   key               — unique dot-notation string; never changes post-seed
#   label             — human-readable name shown in School Admin settings
#   description       — when does this event fire?
#   source_module     — the vs_* app that owns this event
#   supported_channels— list of channel strings this event supports
#   default_enabled   — principled fallback when no setting row exists; also
#                       the value used to seed platform rows
#   is_transactional  — (optional, default False) True bypasses all
#                       NotificationSetting checks; the event always dispatches
#                       on its supported channels (is_active still wins). Use for
#                       password resets, invites, and similar must-send mail.
# ---------------------------------------------------------------------------

# Authoritative seed list for NotificationEventType rows.
EVENT_TYPE_REGISTRY = [

    # ── Academic & Student (vs_students) ───────────────────────────────────

    {
        "key": "student.enrolled",
        "label": "Student enrolled",
        "description": "Fires when a new student record is created and activated in a class.",
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "student.deactivated",
        "label": "Student deactivated",
        "description": "Fires when a student is withdrawn, suspended, or marked inactive.",
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "student.class_transferred",
        "label": "Student class transfer",
        "description": "Fires when a student is moved from one class to another within the current session.",
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "student.promoted",
        "label": "Student promotion batch completed",
        "description": "Fires when a promotion batch job finishes for a branch.",
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },

    # ── Workflow Approval (vs_workflow) ────────────────────────────────────

    {
        "key": "workflow.submitted",
        "label": "Workflow submitted",
        "description": "Fires when a new workflow instance is submitted and awaiting first-stage approval.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "workflow.approved",
        "label": "Workflow stage approved",
        "description": "Fires when a stage is approved and the instance advances to the next stage.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "workflow.rejected",
        "label": "Workflow rejected",
        "description": "Fires when a workflow stage rejection terminates the instance.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "workflow.returned",
        "label": "Workflow returned for revision",
        "description": "Fires when an approver returns an instance to the submitter for changes.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "workflow.final_approved",
        "label": "Workflow fully approved",
        "description": "Fires when the final stage is approved and the workflow instance is complete.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "workflow.escalated",
        "label": "Workflow stage escalated",
        "description": "Fires when a stage timeout triggers an escalation to a new approver.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },

    # ── Finance & Billing (vs_billing) ─────────────────────────────────────

    {
        "key": "billing.invoice_issued",
        "label": "Invoice issued",
        "description": "Fires when a student invoice is generated and issued to the parent or guardian.",
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "billing.payment_received",
        "label": "Payment received",
        "description": "Fires when a payment is confirmed against a student invoice.",
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "billing.invoice_overdue",
        "label": "Invoice overdue",
        "description": "Fires when a student invoice passes its due date without full payment.",
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "billing.refund_processed",
        "label": "Refund processed",
        "description": "Fires when a refund is executed after approval by the finance team.",
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },

    # ── Onboarding & System (vs_onboarding / vs_import / vs_users) ─────────

    {
        "key": "onboarding.step_completed",
        "label": "Onboarding step completed",
        "description": "Fires when a school onboarding checklist step is marked complete.",
        "source_module": "vs_onboarding",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "onboarding.go_live_ready",
        "label": "School ready for go-live",
        "description": "Fires when all onboarding blockers are resolved and the school is ready to go live.",
        "source_module": "vs_onboarding",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "user.invited",
        "label": "User invited",
        "description": (
            "Fires when a staff invitation email is dispatched. EMAIL channel only — "
            "the recipient has no in-app account yet. Transactional: always sent."
        ),
        "source_module": "vs_user",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "user.password_reset",
        "label": "Password reset",
        "description": (
            "Fires when a password reset email is dispatched (self-service or "
            "admin-initiated). EMAIL channel only. Transactional: always sent."
        ),
        "source_module": "vs_user",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "user.account_locked",
        "label": "User account locked",
        "description": "Fires when a user account is locked after repeated failed login attempts.",
        "source_module": "vs_users",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "import.completed",
        "label": "Data import completed",
        "description": "Fires when a data import job finishes successfully.",
        "source_module": "vs_import",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "import.failed",
        "label": "Data import failed",
        "description": "Fires when a data import job fails after exhausting retries.",
        "source_module": "vs_import",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },

    # ── Background tasks (core) ────────────────────────────────────────────
    # These were previously created at runtime by core.tasks_base via
    # get_or_create; registering them here makes seeding authoritative and
    # upserts by key so the runtime creates find an existing row.

    {
        "key": "task.completed",
        "label": "Background task completed",
        "description": "Fires when a background job the user owns finishes successfully.",
        "source_module": "core",
        "supported_channels": [ChannelChoices.IN_APP],
        "default_enabled": True,
    },
    {
        "key": "task.failed",
        "label": "Background task failed",
        "description": "Fires when a background job the user owns fails.",
        "source_module": "core",
        "supported_channels": [ChannelChoices.IN_APP],
        "default_enabled": True,
    },

    # ── Todo / task review (vs_todo) ───────────────────────────────────────
    # Also created at runtime by vs_todo; registered here so seeding is
    # authoritative. Both in-app and email are used by the review-request flow.

    {
        "key": "todo.task_completed",
        "label": "Task completed — review requested",
        "description": "Fires when a self-completed task awaits its reviewer's review.",
        "source_module": "vs_todo",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
]
