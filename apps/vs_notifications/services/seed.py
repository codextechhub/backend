# =============================================================================
# vs_notifications / services / seed.py
#
# Seed helpers called by management commands and by vs_onboarding after a
# new school is provisioned.
#
# All seed functions are idempotent — safe to run repeatedly without
# creating duplicate records.
# =============================================================================

import logging

from ..constants import EVENT_TYPE_REGISTRY

logger = logging.getLogger("vs_notifications.seed")


def seed_event_types() -> dict:
    """
    Create or update all NotificationEventType records defined in
    EVENT_TYPE_REGISTRY (constants.py).

    Uses update_or_create on the `key` field, so:
      - New keys are inserted.
      - Existing keys have their metadata updated (label, description,
        supported_channels, default_enabled, is_active).
      - Records for keys no longer in the registry are NOT deleted —
        set is_active=False manually if retiring an event type.

    Returns:
        {"created": N, "updated": N}
    """
    from ..models import NotificationEventType

    created_count = 0
    updated_count = 0

    for entry in EVENT_TYPE_REGISTRY:
        _, created = NotificationEventType.objects.update_or_create(
            key=entry["key"],
            defaults={
                "label":              entry["label"],
                "description":        entry.get("description", ""),
                "source_module":      entry["source_module"],
                "supported_channels": entry["supported_channels"],
                "default_enabled":    entry.get("default_enabled", True),
                "is_active":          True,
            },
        )
        if created:
            created_count += 1
            logger.info("Created event type: %s", entry["key"])
        else:
            updated_count += 1
            logger.debug("Updated event type: %s", entry["key"])

    logger.info(
        "seed_event_types complete — created: %d, updated: %d",
        created_count, updated_count,
    )
    return {"created": created_count, "updated": updated_count}


def seed_school_settings(school) -> dict:
    """
    Create SchoolNotificationSetting records for a single school.

    For each active NotificationEventType and each of its supported_channels,
    a setting record is created using the event type's default_enabled value.

    Uses get_or_create — existing records are never overwritten.  This means:
      - Running this on an already-configured school is safe.
      - If a School Admin has changed a setting, that change is preserved.
      - New event types added after initial provisioning will be seeded on
        the next run with their default_enabled value.

    Called by:
      - vs_onboarding after a new school is provisioned.
      - seed_notification_settings management command (targeted at one school).

    Args:
        school:  A School model instance.

    Returns:
        {"created": N, "skipped": N}
    """
    from ..models import NotificationEventType, SchoolNotificationSetting

    active_event_types = NotificationEventType.objects.filter(is_active=True)

    created_count = 0
    skipped_count = 0

    for event_type in active_event_types:
        for channel in event_type.supported_channels:
            _, created = SchoolNotificationSetting.objects.get_or_create(
                school=school,
                event_type=event_type,
                channel=channel,
                defaults={"is_enabled": event_type.default_enabled},
            )
            if created:
                created_count += 1
            else:
                skipped_count += 1

    logger.info(
        "seed_school_settings complete for school=%s — created: %d, skipped: %d",
        getattr(school, "slug", school.id),
        created_count,
        skipped_count,
    )
    return {"created": created_count, "skipped": skipped_count}


def seed_notification_templates() -> dict:
    """
    Create default NotificationTemplate records for all active event types
    and their supported channels.

    Uses get_or_create — does NOT overwrite templates that Vision Staff have
    already customised.

    Default templates use all available context variables so Vision Staff can
    see exactly what variables are available when they customise content.

    Returns:
        {"created": N, "skipped": N}
    """
    from ..models import NotificationEventType, NotificationTemplate

    DEFAULT_TEMPLATES = _build_default_templates()

    active_event_types = NotificationEventType.objects.filter(is_active=True)

    created_count = 0
    skipped_count = 0

    for event_type in active_event_types:
        for channel in event_type.supported_channels:
            key = (event_type.key, channel)
            defaults = DEFAULT_TEMPLATES.get(key)
            if defaults is None:
                logger.warning(
                    "No default template defined for event_key=%s channel=%s. Skipping.",
                    event_type.key, channel,
                )
                skipped_count += 1
                continue

            _, created = NotificationTemplate.objects.get_or_create(
                event_type=event_type,
                channel=channel,
                defaults=defaults,
            )
            if created:
                created_count += 1
                logger.info(
                    "Created default template for %s / %s", event_type.key, channel
                )
            else:
                skipped_count += 1

    logger.info(
        "seed_notification_templates complete — created: %d, skipped: %d",
        created_count, skipped_count,
    )
    return {"created": created_count, "skipped": skipped_count}


# ---------------------------------------------------------------------------
# Default template content
# ---------------------------------------------------------------------------

def _build_default_templates() -> dict:
    """
    Returns a dict keyed by (event_key, channel) with subject and body defaults.

    All available context variables are referenced so Vision Staff can see
    the full variable set when they customise.
    """
    from ..constants import ChannelChoices as C

    return {

        # ── student.enrolled ────────────────────────────────────────────────
        ("student.enrolled", C.IN_APP): {
            "subject": "",
            "body": (
                "New student enrolled: {{ student_first_name }} {{ student_last_name }} "
                "({{ student_id }}) has been added to {{ class_name }}, {{ branch_name }}."
            ),
        },
        ("student.enrolled", C.EMAIL): {
            "subject": "New student added to your class — {{ student_first_name }} {{ student_last_name }}",
            "body": (
                "Dear Teacher,\n\n"
                "A new student has been enrolled in {{ class_name }} at {{ branch_name }}.\n\n"
                "Student: {{ student_first_name }} {{ student_last_name }}\n"
                "Student ID: {{ student_id }}\n"
                "Academic session: {{ session_name }}\n\n"
                "Please log in to Vision to view the full student profile.\n\n"
                "{{ school_name }} via CodeX Vision"
            ),
        },

        # ── student.deactivated ─────────────────────────────────────────────
        ("student.deactivated", C.IN_APP): {
            "subject": "",
            "body": (
                "Student deactivated: {{ student_first_name }} {{ student_last_name }} "
                "({{ student_id }}) has been marked inactive. Reason: {{ reason_code }}."
            ),
        },
        ("student.deactivated", C.EMAIL): {
            "subject": "Student record deactivated — {{ student_first_name }} {{ student_last_name }}",
            "body": (
                "A student record has been deactivated on CodeX Vision.\n\n"
                "Student: {{ student_first_name }} {{ student_last_name }}\n"
                "Student ID: {{ student_id }}\n"
                "Reason: {{ reason_code }}\n"
                "Actioned by: {{ deactivated_by_name }}\n\n"
                "{{ school_name }} via CodeX Vision"
            ),
        },

        # ── student.class_transferred ───────────────────────────────────────
        ("student.class_transferred", C.IN_APP): {
            "subject": "",
            "body": (
                "Class transfer: {{ student_first_name }} {{ student_last_name }} "
                "has been moved from {{ from_class_name }} to {{ to_class_name }}."
            ),
        },
        ("student.class_transferred", C.EMAIL): {
            "subject": "Student class transfer — {{ student_first_name }} {{ student_last_name }}",
            "body": (
                "A student has been transferred between classes.\n\n"
                "Student: {{ student_first_name }} {{ student_last_name }}\n"
                "Student ID: {{ student_id }}\n"
                "From class: {{ from_class_name }}\n"
                "To class: {{ to_class_name }}\n"
                "Transferred by: {{ transferred_by_name }}\n\n"
                "{{ school_name }} via CodeX Vision"
            ),
        },

        # ── student.promoted ────────────────────────────────────────────────
        ("student.promoted", C.IN_APP): {
            "subject": "",
            "body": (
                "Promotion complete for {{ branch_name }}: {{ promoted_count }} student(s) "
                "promoted from {{ from_session_name }} to {{ to_session_name }}. "
                "Flagged: {{ flagged_count }}."
            ),
        },
        ("student.promoted", C.EMAIL): {
            "subject": "Student promotion batch completed — {{ branch_name }}",
            "body": (
                "The student promotion batch for {{ branch_name }} has completed.\n\n"
                "From session: {{ from_session_name }}\n"
                "To session: {{ to_session_name }}\n"
                "Promoted: {{ promoted_count }}\n"
                "Flagged: {{ flagged_count }}\n"
                "Batch ID: {{ batch_id }}\n\n"
                "Log in to Vision to review flagged students.\n\n"
                "{{ school_name }} via CodeX Vision"
            ),
        },

        # ── workflow.submitted ──────────────────────────────────────────────
        ("workflow.submitted", C.IN_APP): {
            "subject": "",
            "body": (
                "Approval required: {{ document_type }} — '{{ document_title }}' "
                "submitted by {{ submitter_name }} is awaiting your review at stage '{{ stage_name }}'."
            ),
        },
        ("workflow.submitted", C.EMAIL): {
            "subject": "Action required: {{ document_type }} awaiting your approval",
            "body": (
                "A document has been submitted and requires your approval.\n\n"
                "Document type: {{ document_type }}\n"
                "Title: {{ document_title }}\n"
                "Submitted by: {{ submitter_name }}\n"
                "Current stage: {{ stage_name }}\n\n"
                "Please log in to CodeX Vision to review and act on this request.\n\n"
                "CodeX Vision"
            ),
        },

        # ── workflow.approved ───────────────────────────────────────────────
        ("workflow.approved", C.IN_APP): {
            "subject": "",
            "body": (
                "Stage approved: '{{ document_title }}' was approved by {{ approved_by_name }}. "
                "Moving to stage '{{ next_stage_name }}'."
            ),
        },
        ("workflow.approved", C.EMAIL): {
            "subject": "Approval required at next stage — {{ document_type }}",
            "body": (
                "A document has advanced to the next approval stage.\n\n"
                "Document type: {{ document_type }}\n"
                "Title: {{ document_title }}\n"
                "Approved by: {{ approved_by_name }}\n"
                "Next stage: {{ next_stage_name }}\n\n"
                "Please log in to CodeX Vision to continue the approval process.\n\n"
                "CodeX Vision"
            ),
        },

        # ── workflow.rejected ───────────────────────────────────────────────
        ("workflow.rejected", C.IN_APP): {
            "subject": "",
            "body": (
                "Request rejected: '{{ document_title }}' was rejected by {{ rejected_by_name }}. "
                "Reason: {{ rejection_reason }}."
            ),
        },
        ("workflow.rejected", C.EMAIL): {
            "subject": "Your request has been rejected — {{ document_type }}",
            "body": (
                "Your submitted document has been rejected.\n\n"
                "Document type: {{ document_type }}\n"
                "Title: {{ document_title }}\n"
                "Rejected by: {{ rejected_by_name }}\n"
                "Reason: {{ rejection_reason }}\n\n"
                "Please log in to CodeX Vision for more details.\n\n"
                "CodeX Vision"
            ),
        },

        # ── workflow.returned ───────────────────────────────────────────────
        ("workflow.returned", C.IN_APP): {
            "subject": "",
            "body": (
                "Revision requested: '{{ document_title }}' has been returned by "
                "{{ returned_by_name }} for changes."
            ),
        },
        ("workflow.returned", C.EMAIL): {
            "subject": "Revision requested on your submission — {{ document_type }}",
            "body": (
                "Your submitted document has been returned for revision.\n\n"
                "Document type: {{ document_type }}\n"
                "Title: {{ document_title }}\n"
                "Returned by: {{ returned_by_name }}\n"
                "Comment: {{ return_comment }}\n\n"
                "Please log in to CodeX Vision, make the required changes, and resubmit.\n\n"
                "CodeX Vision"
            ),
        },

        # ── workflow.final_approved ─────────────────────────────────────────
        ("workflow.final_approved", C.IN_APP): {
            "subject": "",
            "body": (
                "Fully approved: '{{ document_title }}' has been approved by "
                "{{ final_approver_name }} and is now complete."
            ),
        },
        ("workflow.final_approved", C.EMAIL): {
            "subject": "Your request has been fully approved — {{ document_type }}",
            "body": (
                "Your submitted document has been fully approved.\n\n"
                "Document type: {{ document_type }}\n"
                "Title: {{ document_title }}\n"
                "Final approver: {{ final_approver_name }}\n\n"
                "Please log in to CodeX Vision to view the outcome.\n\n"
                "CodeX Vision"
            ),
        },

        # ── workflow.escalated ──────────────────────────────────────────────
        ("workflow.escalated", C.IN_APP): {
            "subject": "",
            "body": (
                "Escalation: '{{ document_title }}' at stage '{{ stage_name }}' "
                "has been escalated to {{ escalated_to_name }}."
            ),
        },
        ("workflow.escalated", C.EMAIL): {
            "subject": "Escalated approval required — {{ document_type }}",
            "body": (
                "A document has been escalated to you for approval due to a stage timeout.\n\n"
                "Document type: {{ document_type }}\n"
                "Title: {{ document_title }}\n"
                "Stage: {{ stage_name }}\n"
                "Escalated to: {{ escalated_to_name }}\n\n"
                "Please log in to CodeX Vision to action this request promptly.\n\n"
                "CodeX Vision"
            ),
        },

        # ── billing.invoice_issued ──────────────────────────────────────────
        ("billing.invoice_issued", C.IN_APP): {
            "subject": "",
            "body": (
                "New invoice: ₦{{ invoice_amount }} is due for "
                "{{ student_first_name }} {{ student_last_name }} by {{ due_date }}."
            ),
        },
        ("billing.invoice_issued", C.EMAIL): {
            "subject": "New fee invoice — {{ student_first_name }} {{ student_last_name }}",
            "body": (
                "Dear Parent/Guardian,\n\n"
                "A new invoice has been issued for your child's school fees.\n\n"
                "Student: {{ student_first_name }} {{ student_last_name }}\n"
                "Invoice number: {{ invoice_number }}\n"
                "Amount due: ₦{{ invoice_amount }}\n"
                "Due date: {{ due_date }}\n\n"
                "Pay online: {{ payment_link }}\n\n"
                "{{ school_name }} via CodeX Vision"
            ),
        },

        # ── billing.payment_received ────────────────────────────────────────
        ("billing.payment_received", C.IN_APP): {
            "subject": "",
            "body": (
                "Payment confirmed: ₦{{ amount_paid }} received for "
                "{{ student_first_name }} {{ student_last_name }} on {{ payment_date }}."
            ),
        },
        ("billing.payment_received", C.EMAIL): {
            "subject": "Payment confirmed — {{ student_first_name }} {{ student_last_name }}",
            "body": (
                "Dear Parent/Guardian,\n\n"
                "We have received your payment. Thank you.\n\n"
                "Student: {{ student_first_name }} {{ student_last_name }}\n"
                "Invoice number: {{ invoice_number }}\n"
                "Amount paid: ₦{{ amount_paid }}\n"
                "Payment date: {{ payment_date }}\n"
                "Receipt number: {{ receipt_number }}\n\n"
                "{{ school_name }} via CodeX Vision"
            ),
        },

        # ── billing.invoice_overdue ─────────────────────────────────────────
        ("billing.invoice_overdue", C.IN_APP): {
            "subject": "",
            "body": (
                "Overdue invoice: ₦{{ amount_outstanding }} outstanding for "
                "{{ student_first_name }} {{ student_last_name }} — {{ days_overdue }} day(s) overdue."
                " {{ reminder_message }}"
            ),
        },
        ("billing.invoice_overdue", C.EMAIL): {
            "subject": "Overdue fee invoice — {{ student_first_name }} {{ student_last_name }}",
            "body": (
                "Dear Parent/Guardian,\n\n"
                "This is a reminder that the following invoice is overdue.\n\n"
                "{{ reminder_message }}\n\n"
                "Student: {{ student_first_name }} {{ student_last_name }}\n"
                "Invoice number: {{ invoice_number }}\n"
                "Outstanding amount: ₦{{ amount_outstanding }}\n"
                "Original due date: {{ due_date }}\n"
                "Days overdue: {{ days_overdue }}\n\n"
                "Please make payment as soon as possible to avoid disruption to your child's schooling.\n\n"
                "{{ school_name }} via CodeX Vision"
            ),
        },

        # ── billing.refund_processed ────────────────────────────────────────
        ("billing.refund_processed", C.IN_APP): {
            "subject": "",
            "body": (
                "Refund processed: ₦{{ refund_amount }} refunded for "
                "{{ student_first_name }} {{ student_last_name }}."
            ),
        },
        ("billing.refund_processed", C.EMAIL): {
            "subject": "Refund processed — {{ student_first_name }} {{ student_last_name }}",
            "body": (
                "Dear Parent/Guardian,\n\n"
                "A refund has been processed on your account.\n\n"
                "Student: {{ student_first_name }} {{ student_last_name }}\n"
                "Refund amount: ₦{{ refund_amount }}\n"
                "Original invoice: {{ original_invoice_number }}\n"
                "Processed by: {{ processed_by_name }}\n\n"
                "{{ school_name }} via CodeX Vision"
            ),
        },

        # ── onboarding.step_completed ───────────────────────────────────────
        ("onboarding.step_completed", C.IN_APP): {
            "subject": "",
            "body": (
                "Onboarding update: Step {{ step_number }}/{{ total_steps }} — "
                "'{{ step_name }}' completed by {{ completed_by_name }}."
            ),
        },
        ("onboarding.step_completed", C.EMAIL): {
            "subject": "Onboarding step completed — {{ step_name }}",
            "body": (
                "An onboarding step has been completed for {{ school_name }}.\n\n"
                "Step {{ step_number }} of {{ total_steps }}: {{ step_name }}\n"
                "Completed by: {{ completed_by_name }}\n\n"
                "Log in to the Vision Admin Console to continue.\n\n"
                "CodeX Vision"
            ),
        },

        # ── onboarding.go_live_ready ────────────────────────────────────────
        ("onboarding.go_live_ready", C.IN_APP): {
            "subject": "",
            "body": (
                "{{ school_name }} has completed all onboarding requirements "
                "and is ready to go live."
            ),
        },
        ("onboarding.go_live_ready", C.EMAIL): {
            "subject": "{{ school_name }} is ready to go live",
            "body": (
                "All onboarding requirements have been met for {{ school_name }} ({{ school_slug }}).\n\n"
                "Completed by: {{ completed_by_name }}\n\n"
                "Please log in to the Vision Admin Console to flip the school to Live status.\n\n"
                "CodeX Vision"
            ),
        },

        # ── user.invited (EMAIL only) ───────────────────────────────────────
        ("user.invited", C.EMAIL): {
            "subject": "You have been invited to {{ school_name }} on CodeX Vision",
            "body": (
                "Hello {{ invitee_name }},\n\n"
                "You have been invited to join {{ school_name }} on CodeX Vision "
                "as a {{ role_name }}.\n\n"
                "Click the link below to activate your account. "
                "This link expires in {{ expiry_hours }} hours.\n\n"
                "{{ activation_link }}\n\n"
                "If you did not expect this invitation, please ignore this email.\n\n"
                "CodeX Vision"
            ),
        },

        # ── user.account_locked ─────────────────────────────────────────────
        ("user.account_locked", C.IN_APP): {
            "subject": "",
            "body": (
                "Account locked: Your account was locked on {{ locked_at }} "
                "due to repeated failed login attempts."
            ),
        },
        ("user.account_locked", C.EMAIL): {
            "subject": "Your CodeX Vision account has been locked",
            "body": (
                "Hello {{ user_name }},\n\n"
                "Your CodeX Vision account for {{ school_name }} was locked on {{ locked_at }} "
                "due to repeated failed login attempts.\n\n"
                "To unlock your account, follow the instructions here:\n"
                "{{ unlock_instructions_link }}\n\n"
                "If you did not attempt to log in, please contact your school administrator immediately.\n\n"
                "CodeX Vision"
            ),
        },

        # ── import.completed ────────────────────────────────────────────────
        ("import.completed", C.IN_APP): {
            "subject": "",
            "body": (
                "Import complete: {{ import_type }} — {{ success_count }} records imported "
                "successfully. Errors: {{ error_count }}."
            ),
        },
        ("import.completed", C.EMAIL): {
            "subject": "Data import completed — {{ import_type }}",
            "body": (
                "Your data import has finished.\n\n"
                "Import type: {{ import_type }}\n"
                "Total rows processed: {{ total_rows }}\n"
                "Successful: {{ success_count }}\n"
                "Errors: {{ error_count }}\n"
                "Import ID: {{ import_id }}\n\n"
                "Log in to Vision to review any errors.\n\n"
                "CodeX Vision"
            ),
        },

        # ── import.failed ───────────────────────────────────────────────────
        ("import.failed", C.IN_APP): {
            "subject": "",
            "body": (
                "Import failed: {{ import_type }} could not be completed. "
                "{{ error_summary }}"
            ),
        },
        ("import.failed", C.EMAIL): {
            "subject": "Data import failed — {{ import_type }}",
            "body": (
                "Your data import failed to complete.\n\n"
                "Import type: {{ import_type }}\n"
                "Error summary: {{ error_summary }}\n"
                "Import ID: {{ import_id }}\n\n"
                "Please log in to Vision to review the error report and retry.\n\n"
                "CodeX Vision"
            ),
        },
    }
