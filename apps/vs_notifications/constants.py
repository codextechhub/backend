# =============================================================================
# vs_notifications / constants.py
#
# All enums, TextChoices, and the EVENT_TYPE_REGISTRY that seeds the
# NotificationEventType table.  Adding a new event type means:
#   1. Add an entry to EVENT_TYPE_REGISTRY below.  This list is the only place
#      the catalogue is written down: vs_notifications migration 0008 reads it
#      directly, so a new database picks the entry up with no further step.
#   2. Run: python manage.py seed_notification_event_types  (resyncs databases
#      that already exist; build.sh does this on every deploy)
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

    # Terminal states - no further transitions allowed
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
# Config keys (read via vs_config.conf.get_config at runtime; definitions are
# seeded by vs_config's seed_config_catalogue command)
# ---------------------------------------------------------------------------

# Runtime tuning keys for Celery email delivery behavior.
class NotificationConfigKey:
    EMAIL_MAX_RETRIES       = "notifications.email_max_retries"
    EMAIL_RETRY_BACKOFF_SEC = "notifications.email_retry_backoff_seconds"

    DEFAULTS = {
        EMAIL_MAX_RETRIES:       3,
        EMAIL_RETRY_BACKOFF_SEC: 60,
    }


# ---------------------------------------------------------------------------
# Permission keys
# (must match entries in the vs_rbac seed - communication.* namespace)
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
#   key               - unique dot-notation string; never changes post-seed
#   label             - human-readable name shown in School Admin settings
#   description       - when does this event fire?
#   source_module     - the vs_* app that owns this event
#   supported_channels- list of channel strings this event supports
#   default_enabled   - principled fallback when no setting row exists; also
#                       the value used to seed platform rows
#   is_transactional  - (optional, default False) True bypasses all
#                       NotificationSetting checks; the event always dispatches
#                       on its supported channels (is_active still wins). Use for
#                       password resets, invites, and similar must-send mail.
#   is_active         - (optional, default True) False registers the event but
#                       keeps it OUT of the settings matrix, the admin catalogue
#                       and dispatch. Honesty flag: an event stays inactive until
#                       a domain module actually emits it - flip it on in the
#                       same change that adds the send_notification call.
# ---------------------------------------------------------------------------

# Authoritative seed list for NotificationEventType rows.
EVENT_TYPE_REGISTRY = [

    # Procurement vendor portal. These are must-send external emails, so they
    # bypass per-tenant channel toggles while still retaining delivery history.
    {
        "key": "procurement.purchase_order_issued",
        "label": "Vendor purchase order",
        "description": "Sends a fully approved purchase order and its PDF to a vendor.",
        "source_module": "vs_procurement",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "procurement.rfq_invitation",
        "label": "Vendor RFQ invitation",
        "description": "Invites a vendor contact to respond to an issued RFQ.",
        "source_module": "vs_procurement",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "procurement.rfq_verification_code",
        "label": "Vendor RFQ verification code",
        "description": "Verifies a vendor contact before opening a quotation form.",
        "source_module": "vs_procurement",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "procurement.rfq_reminder",
        "label": "Vendor RFQ reminder",
        "description": "Reminds a vendor that an RFQ response is outstanding.",
        "source_module": "vs_procurement",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "procurement.quotation_receipt",
        "label": "Vendor quotation receipt",
        "description": "Confirms a firm vendor quotation submission.",
        "source_module": "vs_procurement",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "procurement.rfq_amended",
        "label": "Vendor RFQ amendment",
        "description": "Notifies a vendor that an issued RFQ changed.",
        "source_module": "vs_procurement",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "procurement.rfq_deadline_extended",
        "label": "Vendor RFQ deadline extension",
        "description": "Notifies a vendor of an invitation-specific deadline extension.",
        "source_module": "vs_procurement",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },

    # ── Support Tickets (vs_tickets) ───────────────────────────────────────

    {
        "key": "ticket.created",
        "label": "Ticket created",
        "description": "Fires when a user creates a support ticket.",
        "source_module": "vs_tickets",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "ticket.assigned",
        "label": "Ticket assigned",
        "description": "Fires when a ticket is assigned to support staff.",
        "source_module": "vs_tickets",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "ticket.status_changed",
        "label": "Ticket status changed",
        "description": "Fires when a ticket changes workflow status.",
        "source_module": "vs_tickets",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "ticket.commented",
        "label": "Ticket commented",
        "description": "Fires when a visible comment is added to a ticket.",
        "source_module": "vs_tickets",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "ticket.resolved",
        "label": "Ticket resolved",
        "description": "Fires when a ticket is marked resolved.",
        "source_module": "vs_tickets",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "ticket.closed",
        "label": "Ticket closed",
        "description": "Fires when a ticket is closed.",
        "source_module": "vs_tickets",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "ticket.reopened",
        "label": "Ticket reopened",
        "description": "Fires when a closed ticket is reopened.",
        "source_module": "vs_tickets",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "ticket.attachment_added",
        "label": "Ticket attachment added",
        "description": "Fires when a file is attached to a ticket.",
        "source_module": "vs_tickets",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },

    # ── Academic & Student (vs_students) ───────────────────────────────────

    {
        "key": "student.enrolled",
        "label": "Student enrolled",
        "description": "Fires when a new student record is created and activated in a class.",
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # no vs_students emitter yet
    },
    {
        "key": "student.deactivated",
        "label": "Student deactivated",
        "description": "Fires when a student is withdrawn, suspended, or marked inactive.",
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # no vs_students emitter yet
    },
    {
        "key": "student.class_transferred",
        "label": "Student class transfer",
        "description": "Fires when a student is moved from one class to another within the current session.",
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # no vs_students emitter yet
    },
    {
        "key": "student.promoted",
        "label": "Student promotion batch completed",
        "description": "Fires when a promotion batch job finishes for a branch.",
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # no vs_students emitter yet
    },

    # ── Workflow Approval (vs_workflow) ────────────────────────────────────
    # Wired lifecycle points (services/routing.py): stage_activated → the
    # stage's approvers; returned / rejected / final_approved → the requester.
    # The rest are registered inactive until the engine emits them.

    {
        "key": "workflow.stage_activated",
        "label": "Approval awaiting your decision",
        "description": "Fires when an approval stage becomes active and you are one of its approvers.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "workflow.submitted",
        "label": "Workflow submitted",
        "description": "Fires when a new workflow instance is submitted and awaiting first-stage approval.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # superseded by workflow.stage_activated (first stage)
    },
    {
        "key": "workflow.approved",
        "label": "Workflow stage approved",
        "description": "Fires when a stage is approved and the instance advances to the next stage.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # superseded by workflow.stage_activated (next stage)
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
        "is_active": False,  # engine has no escalation emitter yet
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
        "key": "billing.statement_issued",
        "label": "Statement of account",
        "description": "Sends a customer their statement of account with a PDF copy attached.",
        "source_module": "vs_finance",
        # Email only: the recipient is a paying customer who has no console account,
        # so there is no in-app inbox to deliver a statement to.
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "billing.debit_note_issued",
        "label": "Debit note issued",
        "description": "Fires when a posted debit note adds a charge to a customer's account.",
        "source_module": "vs_finance",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "billing.credit_note_issued",
        "label": "Credit note issued",
        "description": "Fires when a posted credit note reduces a customer's account balance.",
        "source_module": "vs_finance",
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
    # Payment gateway operations (vs_payments). Money has already moved at the
    # provider by the time either of these fires, so they are operational alarms
    # rather than customer messages: the audience is whoever can replay the event.
    {
        "key": "payments.unbooked_receipts_digest",
        "label": "Unbooked gateway receipts",
        "description": (
            "Daily summary of provider events that could not be booked, so money "
            "sitting outside the books is noticed rather than waiting to be found."
        ),
        "source_module": "vs_payments",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "payments.unbooked_receipts_surge",
        "label": "Gateway bookings failing",
        "description": (
            "Fires when several provider events fail to book inside one window, "
            "which points at a systemic cause rather than one bad payment."
        ),
        "source_module": "vs_payments",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        # A surge means bookings are broken right now. Someone muting the daily
        # digest should still hear about that, so it must not be silenceable.
        "is_transactional": True,
    },

    {
        "key": "billing.refund_processed",
        "label": "Refund processed",
        "description": "Fires when a refund is executed after approval by the finance team.",
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # refund flow does not emit yet
    },

    # Platform health alerts are operational alarms, not configurable product
    # updates. Both destinations must be created whenever a rule starts firing.
    {
        "key": "health.alert_fired",
        "label": "Platform health alert",
        "description": (
            "Notifies platform health operators when a sustained alert rule breach "
            "opens an incident."
        ),
        "source_module": "vs_health",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
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
        "key": "onboarding.go_live_reviewed",
        "label": "Go-live request reviewed",
        "description": (
            "Fires when platform staff approve or reject a school's go-live "
            "request, carrying the decision, the reviewer and the reason when "
            "the request was rejected."
        ),
        "source_module": "vs_onboarding",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "onboarding.activated",
        "label": "School activated",
        "description": "Fires when a school is taken live and the rest of the platform opens to it.",
        "source_module": "vs_onboarding",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "onboarding.expiry_warning",
        "label": "Onboarding expiring soon",
        "description": (
            "Fires once, fourteen days before a school's onboarding window "
            "closes, telling the school when it expires and how long is left. "
            "Goes to the school, not to platform staff."
        ),
        "source_module": "vs_onboarding",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        # A school that mutes onboarding chatter must still be told its access
        # is about to stop: this is the last notice before the sign-in closes.
        "is_transactional": True,
    },
    {
        "key": "onboarding.stale_report",
        "label": "Stale onboarding report",
        "description": (
            "Fires every two weeks with the schools that have been onboarding "
            "too long and the ones the expiry sweep has just suspended. Goes to "
            "platform operators, never to the school."
        ),
        "source_module": "vs_onboarding",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "user.invited",
        "label": "User invited",
        "description": (
            "Fires when a staff invitation email is dispatched. EMAIL channel only - "
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
        "is_active": False,  # lockout flow does not emit yet
    },
    {
        "key": "import.completed",
        "label": "Data import completed",
        "description": "Fires when a data import job finishes successfully.",
        "source_module": "vs_import",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # imports report via task.completed instead
    },
    {
        "key": "import.failed",
        "label": "Data import failed",
        "description": "Fires when a data import job fails after exhausting retries.",
        "source_module": "vs_import",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # imports report via task.failed instead
    },

    # ── Export Centre (vs_exports) ─────────────────────────────────────────
    # Emitted by vs_exports.services._notify on every terminal run. Active,
    # unlike the import pair above, because the Export Centre does NOT report
    # through task.completed/task.failed: a background job that succeeded can
    # still have produced a file with columns left out, and only the export
    # event carries that distinction.
    {
        "key": "export.run_completed",
        "label": "Export ready",
        "description": (
            "Fires when an export run finishes and its file is available. Also "
            "covers a run that completed with omissions, where `error` explains "
            "what was left out."
        ),
        "source_module": "vs_exports",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": True,
    },
    {
        "key": "export.run_failed",
        "label": "Export failed",
        "description": (
            "Fires when an export run fails and no file was produced. `error` "
            "carries the user-safe reason and the recommended action."
        ),
        "source_module": "vs_exports",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": True,
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
        "label": "Task completed - review requested",
        "description": "Fires when a self-completed task awaits its reviewer's review.",
        "source_module": "vs_todo",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
]
