"""All enums, TextChoices, and the EVENT_TYPE_REGISTRY that seeds the
NotificationEventType table.  Adding a new event type means:
  1. Add an entry to EVENT_TYPE_REGISTRY below.  This list is the only place
     the catalogue is written down: vs_notifications migration 0008 reads it
     directly, so a new database picks the entry up with no further step.
  2. Run: python manage.py seed_notification_event_types  (resyncs databases
     that already exist; build.sh does this on every deploy)
  3. Run: python manage.py seed_notification_templates   (creates default body)

Each registry entry defines one ``NotificationEventType`` row:

``key``
    Unique dot-notation string, never changed after seeding.
``label``
    Human-readable name shown in School Admin settings.
``description``
    When the event fires.
``source_module``
    The ``vs_*`` app that owns the event.
``supported_channels``
    The channel strings the event supports.
``default_enabled``
    The fallback when no setting row exists, and the value platform rows are
    seeded with.
``is_transactional``
    Optional, default False. True bypasses every ``NotificationSetting``
    check and always dispatches on the supported channels, ``is_active``
    still winning. For password resets, invitations and other must-send mail.
``branch_scoped``
    Optional, default False. True when every place that sends the event
    passes ``branch=``, the branch the event is about, so a branch may switch
    the event's channels on or off for itself. Set it in the same change that
    wires the last sender; an event some sender fires without a branch stays a
    whole-tenant setting, because a branch choice it could not honour would be
    a switch that does nothing.
``is_active``
    Optional, default True. False registers the event but keeps it out of the
    settings matrix, the admin catalogue and dispatch. It is an honesty flag:
    an event stays inactive until a domain module actually emits it, and is
    switched on in the same change that adds the ``send_notification`` call.
"""
# ---------------------------------------------------------------------------
# Channel choices
# ---------------------------------------------------------------------------

# Persisted delivery channel values used by settings, templates, and notifications.
class ChannelChoices:
    IN_APP = "in_app"
    EMAIL  = "email"
    # Web Push to a phone or tablet the recipient installed an app on and
    # allowed notifications for (services/push.py). Text that may show on a
    # locked screen, so a push template says what happened and never a figure.
    PUSH   = "push"

    CHOICES = [
        (IN_APP, "In-App"),
        (EMAIL,  "Email"),
        (PUSH,   "Push"),
    ]

    ALL = [IN_APP, EMAIL, PUSH]


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
    BRANCH_NOT_CONFIGURABLE               = "BRANCH_NOT_CONFIGURABLE"
    RESET_NEEDS_BRANCH                    = "RESET_NEEDS_BRANCH"
    BRANCH_SCOPE_REQUIRED                 = "BRANCH_SCOPE_REQUIRED"
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
# Event type registry. See the module docstring for the field contract.
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
        "key": "procurement.quotation_submitted",
        "label": "Vendor quotation submitted",
        "description": "Alerts authorized buyers when a vendor submits or revises a quotation.",
        "source_module": "vs_procurement",
        "supported_channels": [ChannelChoices.IN_APP],
        "default_enabled": True,
        "branch_scoped": True,
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
        "key": "ticket.escalated",
        "label": "Ticket escalated to CodeX",
        "description": (
            "Fires when a school hands one of its own tickets up to CodeX "
            "support. Addressed to the platform desk, which is the audience "
            "that gains the ticket at that moment."
        ),
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
        "key": "student.suspended",
        "label": "Student suspended",
        "description": (
            "Fires when a student is suspended, and carries the notice the "
            "school's guardians receive. Who is written to is the school's own "
            "setting, students.suspension.notice."
        ),
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
    },
    {
        "key": "student.deactivated",
        "label": "Student deactivated",
        "description": (
            "Fires when a student is withdrawn or marked inactive. A "
            "suspension is student.suspended, which a guardian reads and this "
            "does not."
        ),
        "source_module": "vs_students",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_active": False,  # no vs_students emitter
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

    # ── Attendance (vs_attendance) ─────────────────────────────────────────
    # Every sender passes the register's branch, so a branch may switch these
    # on or off for itself.

    {
        "key": "attendance.absence_notice",
        "label": "Absence notice to guardian",
        "description": (
            "Fires when a class register is submitted with a child marked "
            "absent, and is sent to the child's primary guardian. Whether it "
            "is sent at all is the school's setting, attendance.guardian_notices."
        ),
        "source_module": "vs_attendance",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "attendance.correction_notice",
        "label": "Attendance correction notice to guardian",
        "description": (
            "Fires when a mark whose absence notice was sent is changed to "
            "something else, and is sent to the guardian who received the "
            "absence notice."
        ),
        "source_module": "vs_attendance",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "attendance.register_reminder",
        "label": "Register not yet taken",
        "description": (
            "Fires when an administrator reminds a class teacher that the "
            "class's register for a day has not been taken."
        ),
        "source_module": "vs_attendance",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },

    # ── Gradebook (vs_gradebook) ───────────────────────────────────────────
    # Every sender passes the sheet's or the class's branch, so a branch may
    # switch these on or off for itself. The two report card events go to a
    # child's primary guardian and carry nothing from the result.

    {
        "key": "gradebook.sheet_reminder",
        "label": "Score sheet not yet in",
        "description": (
            "Fires when an administrator reminds a subject's main teacher that "
            "the score sheet for a class has not been submitted."
        ),
        "source_module": "vs_gradebook",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "gradebook.sheet_returned",
        "label": "Score sheet returned",
        "description": (
            "Fires when the head of a department returns a moderated score "
            "sheet to its main teacher with a note, and is sent to that teacher."
        ),
        "source_module": "vs_gradebook",
        "supported_channels": [ChannelChoices.IN_APP],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "gradebook.class_returned",
        "label": "Class results returned",
        "description": (
            "Fires when the principal returns a class's results to its class "
            "teacher with a note, and is sent to the class teacher."
        ),
        "source_module": "vs_gradebook",
        "supported_channels": [ChannelChoices.IN_APP],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "gradebook.report_card_ready",
        "label": "Report card ready to collect",
        "description": (
            "Fires when a class's results are published and a child's result is "
            "released, and is sent to the child's primary guardian. It says only "
            "that the report card is ready: no score, grade, position or comment. "
            "A guardian with a parent app account also gets it as a push, which "
            "opens the child's results there."
        ),
        "source_module": "vs_gradebook",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL, ChannelChoices.PUSH],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "gradebook.result_released_parent",
        "label": "Your child's result is ready",
        "description": (
            "Fires when a child's result is released at a school that tells every "
            "parent (gradebook.results_tell_every_parent), and is sent to the "
            "child's other parents with a parent account: in the parent app and "
            "as a push that opens the child's results. Never emailed, so a family "
            "gets one report card email, the primary guardian's. No score, grade, "
            "position or comment."
        ),
        "source_module": "vs_gradebook",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.PUSH],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "gradebook.result_released",
        "label": "Your result is ready",
        "description": (
            "Fires when a class's results are published and a pupil's own result "
            "is released, and is sent to the pupil when they have a portal "
            "account. It says only that the result for the term is ready to read: "
            "no score, grade or position."
        ),
        "source_module": "vs_gradebook",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.PUSH],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "gradebook.report_card_updated",
        "label": "Updated report card ready to collect",
        "description": (
            "Fires when a correction to a released result is approved, and is "
            "sent to the child's primary guardian. It says only that an updated "
            "report card is ready to collect from the school."
        ),
        "source_module": "vs_gradebook",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },

    # ── Notices (vs_notices) ───────────────────────────────────────────────
    {
        "key": "notices.published",
        "label": "New notice",
        "description": (
            "Fires when a school publishes a notice, and is sent to every pupil "
            "in its audience who has a portal account."
        ),
        "source_module": "vs_notices",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL, ChannelChoices.PUSH],
        "default_enabled": True,
    },

    {
        "key": "notices.published_parent",
        "label": "New notice for your child",
        "description": (
            "Fires when a school publishes a notice that reaches a child, and "
            "is sent once to each parent with an account, naming their "
            "children it reaches. Whether it is sent at all is the school's "
            "setting, notices.tell_parents. As a push it opens the notice in the "
            "parent app."
        ),
        "source_module": "vs_notices",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL, ChannelChoices.PUSH],
        "default_enabled": True,
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
        "branch_scoped": True,
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
        "branch_scoped": True,
    },
    {
        "key": "workflow.returned",
        "label": "Workflow returned for revision",
        "description": "Fires when an approver returns an instance to the submitter for changes.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "workflow.final_approved",
        "label": "Workflow fully approved",
        "description": "Fires when the final stage is approved and the workflow instance is complete.",
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
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
    {
        "key": "workflow.approver_removed",
        "label": "Approval no longer needs you",
        "description": (
            "Fires when an administrator, or a revoked delegation, takes you off "
            "an approval that was waiting for your decision."
        ),
        "source_module": "vs_workflow",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },

    # ── Finance & Billing (vs_billing) ─────────────────────────────────────

    {
        "key": "billing.invoice_issued",
        "label": "Invoice issued",
        "description": "Fires when a student invoice is generated and issued to the parent or guardian.",
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
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
        "branch_scoped": True,
    },
    {
        "key": "billing.debit_note_issued",
        "label": "Debit note issued",
        "description": "Fires when a posted debit note adds a charge to a customer's account.",
        "source_module": "vs_finance",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "billing.credit_note_issued",
        "label": "Credit note issued",
        "description": "Fires when a posted credit note reduces a customer's account balance.",
        "source_module": "vs_finance",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "billing.payment_received",
        "label": "Payment received",
        "description": "Fires when a payment is confirmed against a student invoice.",
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL, ChannelChoices.PUSH],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "billing.transfer_account_changed",
        "label": "Your account number changed",
        "description": (
            "Fires when a pupil's account number for school fees is replaced: the "
            "provider closed it or gave it to somebody else, or the school's money "
            "started settling to its own bank. Sent to the pupil when they have a "
            "portal account, so they stop paying into the old number. The push "
            "carries no number. Always sent: a school cannot switch it off."
        ),
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.PUSH],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "billing.guardian_transfer_account_changed",
        "label": "Your child's account number changed",
        "description": (
            "Fires with \"Your account number changed\" and goes to the pupil's "
            "primary guardian. It names the old number, says not to pay into it, "
            "and sends them to the pupil's app or their own for the new one. It "
            "never carries the new number, and says the school never sends one by "
            "email, so a copy of it with a stranger's number reads as a fraud. "
            "Always sent: a school cannot switch it off."
        ),
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "is_transactional": True,
    },
    {
        "key": "billing.guardian_invoice_issued",
        "label": "New bill for your child",
        "description": (
            "Fires when the school raises a fee for a pupil, by billing a class or "
            "one pupil, and goes to the pupil's primary guardian when they have a "
            "parent app account: in the app, and as a push that opens the child's "
            "Fees. Their email copy, with the PDF, is the invoice email itself, "
            "which reaches the primary guardian as well as any billing address. A "
            "second parent sees the bill in their own parent app without being "
            "sent it. An opening balance carried into the books is not sent."
        ),
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.PUSH],
        "default_enabled": True,
    },
    {
        "key": "billing.invoice_overdue",
        "label": "Invoice overdue",
        "description": "Fires when a student invoice passes its due date without full payment.",
        "source_module": "vs_billing",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
    },
    # Finance operations (vs_finance). Every posting needs a fiscal period covering
    # its date, so a calendar about to run out is an outage on a known date.
    {
        "key": "finance.fiscal_calendar_expiring",
        "label": "Fiscal calendar running out",
        "description": (
            "Tells the staff who may open a fiscal year that postings will stop on "
            "a date no fiscal period covers, because the calendar is ending or has "
            "a gap, and the next year was not opened automatically."
        ),
        "source_module": "vs_finance",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        # Muting this would leave the entity to find out when every posting fails.
        "is_transactional": True,
    },
    # Payslips (vs_finance). The tenant's payroll settings decide which of the
    # two is sent, so each is its own event with one channel.
    {
        "key": "payroll.payslip_ready",
        "label": "Payslip ready",
        "description": "Tells an employee their payslip for a pay period is ready to open.",
        "source_module": "vs_finance",
        "supported_channels": [ChannelChoices.IN_APP],
        "default_enabled": True,
        "branch_scoped": True,
    },
    {
        "key": "payroll.payslip_emailed",
        "label": "Payslip by email",
        "description": "Emails an employee their payslip as a PDF when their pay is sent.",
        "source_module": "vs_finance",
        "supported_channels": [ChannelChoices.EMAIL],
        "default_enabled": True,
        "branch_scoped": True,
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
        "key": "payments.dispute_received",
        "label": "Chargeback or provider refund",
        "description": (
            "A payer's bank disputed an online payment, or a refund was made at the "
            "payment provider. It is recorded against the payment and never booked "
            "automatically, so finance staff decide how the loss is borne."
        ),
        "source_module": "vs_payments",
        "supported_channels": [ChannelChoices.IN_APP, ChannelChoices.EMAIL],
        "default_enabled": True,
        # A dispute has a deadline at the provider; muting it forfeits the reply.
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

    # Emitted by vs_exports.services._notify on every terminal run. Active,
    # unlike the import pair above, because the Export Centre does not report
    # through task.completed/task.failed: a job that succeeded can still have
    # produced a file with columns left out, and only the export event carries
    # that distinction.
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
