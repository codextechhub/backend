"""Give every in-app notification template a subject of its own.

The in-app tray prints a template's subject above its body, and the feed
serializer falls back to the event type's label when that subject is blank,
which puts the category in the headline and again in the subscript beneath it.
This fills the subject of every in-app template that has none, and refreshes
each body so it carries the detail rather than repeating the headline.

Only platform-maintained copy is touched, and only on the in-app channel. A
subject somebody has written is never replaced, and a body is replaced only
while it still matches the default it shipped with. Email templates, HTML and
actions are left alone; an in-app template has none of the last three.
"""

from django.db import migrations


#: event key -> the copy to adopt, and the default body it replaces. A stored
#: body that differs from ``previous_body`` was edited by a person and is kept.
TEMPLATES = {
    "ticket.created": {
        "subject": "New ticket {{ ticket_number }}: {{ ticket_title }}",
        "body": "Raised by {{ requester_name }}. Priority: {{ ticket_priority }}.",
        "previous_body": "New ticket {{ ticket_number }}: {{ ticket_title }} was created by {{ requester_name }}.",
    },
    "ticket.assigned": {
        "subject": "{{ ticket_number }} is yours: {{ ticket_title }}",
        "body": "{% if actor_name %}{{ actor_name }} assigned it to you. {% endif %}Raised by {{ requester_name }}. Priority: {{ ticket_priority }}.",
        "previous_body": "Ticket {{ ticket_number }} has been assigned to you.",
    },
    "ticket.status_changed": {
        "subject": "{{ ticket_number }}: {{ ticket_title }}",
        "body": "Moved from {{ old_status }} to {{ new_status }}{% if actor_name %} by {{ actor_name }}{% endif %}.",
        "previous_body": "Ticket {{ ticket_number }} moved from {{ old_status }} to {{ new_status }}.",
    },
    "ticket.escalated": {
        "subject": "{{ ticket_number }} escalated: {{ ticket_title }}",
        "body": "{% if school_name %}{{ school_name }} handed it to CodeX{% else %}Handed to CodeX{% endif %}{% if actor_name %} by {{ actor_name }}{% endif %}. Priority: {{ ticket_priority }}.",
        "previous_body": "{{ school_name }} escalated ticket {{ ticket_number }} to CodeX: {{ ticket_title }}",
    },
    "ticket.commented": {
        "subject": "{% if actor_name %}{{ actor_name }} commented on {{ ticket_number }}{% else %}New comment on {{ ticket_number }}{% endif %}",
        "body": "{{ comment_body }}",
        "previous_body": "{{ actor_name }} commented on ticket {{ ticket_number }}: {{ comment_body }}",
    },
    "ticket.resolved": {
        "subject": "{{ ticket_number }} resolved: {{ ticket_title }}",
        "body": "{% if actor_name %}Resolved by {{ actor_name }}. {% endif %}Reopen it if the problem is still there.",
        "previous_body": "Ticket {{ ticket_number }} has been resolved.",
    },
    "ticket.closed": {
        "subject": "{{ ticket_number }} closed: {{ ticket_title }}",
        "body": "{% if actor_name %}Closed by {{ actor_name }}. {% endif %}Raise a new ticket if you need more help.",
        "previous_body": "Ticket {{ ticket_number }} has been closed.",
    },
    "ticket.reopened": {
        "subject": "{{ ticket_number }} reopened: {{ ticket_title }}",
        "body": "{% if actor_name %}Reopened by {{ actor_name }}. {% endif %}It is back in the queue at priority {{ ticket_priority }}.",
        "previous_body": "Ticket {{ ticket_number }} has been reopened.",
    },
    "ticket.attachment_added": {
        "subject": "{{ attachment_name }} added to {{ ticket_number }}",
        "body": "{% if actor_name %}{{ actor_name }} attached it{% else %}Attached{% endif %} to {{ ticket_title }}.",
        "previous_body": "{{ actor_name }} attached {{ attachment_name }} to ticket {{ ticket_number }}.",
    },
    "student.enrolled": {
        "subject": "{{ student_first_name }} {{ student_last_name }} {% if class_name %}joined {{ class_name }}{% else %}was enrolled{% endif %}",
        "body": "Student ID {{ student_id }}, {{ branch_name }}.",
        "previous_body": "New student enrolled: {{ student_first_name }} {{ student_last_name }} ({{ student_id }}) has been added to {{ class_name }}, {{ branch_name }}.",
    },
    "student.deactivated": {
        "subject": "{{ student_first_name }} {{ student_last_name }} is no longer active",
        "body": "Student ID {{ student_id }}. Reason: {{ reason_code }}.",
        "previous_body": "Student deactivated: {{ student_first_name }} {{ student_last_name }} ({{ student_id }}) has been marked inactive. Reason: {{ reason_code }}.",
    },
    "student.class_transferred": {
        "subject": "{{ student_first_name }} {{ student_last_name }} moved to {{ to_class_name }}",
        "body": "Previously in {{ from_class_name }}. Moved by {{ transferred_by_name }}.",
        "previous_body": "Class transfer: {{ student_first_name }} {{ student_last_name }} has been moved from {{ from_class_name }} to {{ to_class_name }}.",
    },
    "student.promoted": {
        "subject": "{{ promoted_count }} student{{ promoted_count|pluralize }} promoted at {{ branch_name }}",
        "body": "{{ from_session_name }} to {{ to_session_name }}. {{ flagged_count }} flagged for review.",
        "previous_body": "Promotion complete for {{ branch_name }}: {{ promoted_count }} student(s) promoted from {{ from_session_name }} to {{ to_session_name }}. Flagged: {{ flagged_count }}.",
    },
    "workflow.stage_activated": {
        "subject": "{{ document_title }} needs your approval",
        "body": "Submitted by {{ submitter_name }}, awaiting your decision at {{ stage_name }}.",
        "previous_body": "Approval required: {{ document_title }} submitted by {{ submitter_name }} is awaiting your decision at stage '{{ stage_name }}'.",
    },
    "workflow.submitted": {
        "subject": "{{ document_title }} needs your review",
        "body": "{{ document_type }} submitted by {{ submitter_name }}, waiting at {{ stage_name }}.",
        "previous_body": "Approval required: {{ document_type }} - '{{ document_title }}' submitted by {{ submitter_name }} is awaiting your review at stage '{{ stage_name }}'.",
    },
    "workflow.approved": {
        "subject": "{{ document_title }} moved to {{ next_stage_name }}",
        "body": "Approved by {{ approved_by_name }}.",
        "previous_body": "Stage approved: '{{ document_title }}' was approved by {{ approved_by_name }}. Moving to stage '{{ next_stage_name }}'.",
    },
    "workflow.rejected": {
        "subject": "{{ document_title }} was rejected",
        "body": "{% if rejected_by_name %}Rejected by {{ rejected_by_name }}. {% endif %}{% if rejection_reason %}Reason: {{ rejection_reason }}{% else %}No reason was recorded{% endif %}.",
        "previous_body": "Request rejected: '{{ document_title }}' was rejected by {{ rejected_by_name }}. Reason: {{ rejection_reason }}.",
    },
    "workflow.returned": {
        "subject": "{{ document_title }} needs changes",
        "body": "{% if returned_by_name %}Returned by {{ returned_by_name }}. {% endif %}{% if return_comment %}{{ return_comment }}{% else %}No comment was left. Revise it and resubmit{% endif %}.",
        "previous_body": "Revision requested: '{{ document_title }}' has been returned by {{ returned_by_name }} for changes.",
    },
    "workflow.escalated": {
        "subject": "{{ document_title }} escalated to {{ escalated_to_name }}",
        "body": "{{ stage_name }} timed out, so the decision moved on.",
        "previous_body": "Escalation: '{{ document_title }}' at stage '{{ stage_name }}' has been escalated to {{ escalated_to_name }}.",
    },
    "billing.invoice_issued": {
        "subject": "Invoice {{ invoice_number }} for {{ customer_name }}",
        "body": "₦{{ invoice_amount }} due by {{ due_date }}.",
        "previous_body": "New invoice: ₦{{ invoice_amount }} is due for {{ customer_name }} by {{ due_date }}.",
    },
    "billing.debit_note_issued": {
        "subject": "Debit note {{ note_number }} for {{ customer_name }}",
        "body": "₦{{ note_amount }} added. {{ current_balance_label }}: ₦{{ current_balance_amount }}.",
        "previous_body": "Debit note {{ note_number }} added ₦{{ note_amount }} to {{ customer_name }}'s account. {{ current_balance_label }}: ₦{{ current_balance_amount }}.",
    },
    "billing.credit_note_issued": {
        "subject": "Credit note {{ note_number }} for {{ customer_name }}",
        "body": "₦{{ note_amount }} credited. {{ current_balance_label }}: ₦{{ current_balance_amount }}.",
        "previous_body": "Credit note {{ note_number }} reduced {{ customer_name }}'s account by ₦{{ note_amount }}. {{ current_balance_label }}: ₦{{ current_balance_amount }}.",
    },
    "billing.payment_received": {
        "subject": "₦{{ amount_paid }} received from {{ customer_name }}",
        "body": "Receipt {{ receipt_number }}, {{ payment_date }}{% if invoice_number %}, applied to {{ invoice_number }}{% endif %}.",
        "previous_body": "Payment confirmed: ₦{{ amount_paid }} received for {{ customer_name }} on {{ payment_date }}.",
    },
    "payments.unbooked_receipts_digest": {
        "subject": "{{ total_amount_naira }} has not reached the books at {{ entity_code }}",
        "body": "{{ count }} gateway payment{{ count|pluralize }}, oldest {{ oldest }}.{% if reason %} {{ reason }}{% endif %}",
        "previous_body": "{{ count }} gateway payment(s) totalling at least {{ total_amount_naira }} have not reached the books for {{ entity_code }}. Oldest: {{ oldest }}. {{ reason }}",
    },
    "payments.unbooked_receipts_surge": {
        "subject": "{{ count }} gateway booking{{ count|pluralize }} failed in {{ window_minutes }} minutes",
        "body": "{{ total_amount_naira }} affected across {{ entities }}.{% if reason %} {{ reason }}{% endif %}",
        "previous_body": "{{ count }} gateway payment(s) failed to book in the last {{ window_minutes }} minutes. {{ reason }}",
    },
    "billing.invoice_overdue": {
        "subject": "Invoice {{ invoice_number }} is {{ days_overdue }} day{{ days_overdue|pluralize }} overdue",
        "body": "₦{{ amount_outstanding }} outstanding for {{ customer_name }}, due {{ due_date }}.{% if reminder_message %} {{ reminder_message }}{% endif %}",
        "previous_body": "Overdue invoice: ₦{{ amount_outstanding }} outstanding for {{ customer_name }} - {{ days_overdue }} day(s) overdue. {{ reminder_message }}",
    },
    "billing.refund_processed": {
        "subject": "₦{{ refund_amount }} refunded to {{ customer_name }}",
        "body": "Against invoice {{ original_invoice_number }}. Processed by {{ processed_by_name }}.",
        "previous_body": "Refund processed: ₦{{ refund_amount }} refunded for {{ customer_name }}.",
    },
    "onboarding.step_completed": {
        "subject": "{{ step_name }} is done ({{ step_number }} of {{ total_steps }})",
        "body": "{{ completed_by_name }} marked it complete for {{ school_name }}.",
        "previous_body": "Onboarding update: Step {{ step_number }}/{{ total_steps }} - '{{ step_name }}' completed by {{ completed_by_name }}.",
    },
    "onboarding.go_live_ready": {
        "subject": "{{ school_name }} is ready to request go-live",
        "body": "Every required step is complete, the last by {{ completed_by_name }}. Confirm a date and submit the request.",
        "previous_body": "{{ school_name }} has completed all onboarding requirements and is ready to go live.",
    },
    "onboarding.go_live_reviewed": {
        "subject": "Go-live for {{ school_name }} was {{ decision }}",
        "body": "Reviewed by {{ reviewed_by_name }} on {{ reviewed_at }}.{% if rejection_reason %} Reason: {{ rejection_reason }}{% endif %}",
        "previous_body": "Go-live request for {{ school_name }} was {{ decision }} by {{ reviewed_by_name }} on {{ reviewed_at }}.{% if rejection_reason %} Reason: {{ rejection_reason }}{% endif %}",
    },
    "onboarding.activated": {
        "subject": "{{ school_name }} is now live",
        "body": "Every module is open. Your team can sign in and use what their roles allow.",
        "previous_body": "{{ school_name }} is now live. Every module is open to this school.",
    },
    "onboarding.expiry_warning": {
        "subject": "{{ school_name }} onboarding expires on {{ expires_on }}",
        "body": "{{ days_remaining }} day{{ days_remaining|pluralize }} left. Complete your remaining steps and request go-live before then.",
        "previous_body": "Onboarding for {{ school_name }} expires on {{ expires_on }}, in {{ days_remaining }} day(s). Complete your remaining steps and request go-live before then.",
    },
    "onboarding.stale_report": {
        "subject": "{{ ageing_count }} school{{ ageing_count|pluralize }} ageing, {{ expired_count }} suspended",
        "body": "Ageing means onboarding for more than {{ stale_after_days }} days. Suspensions cover the last {{ window_days }} days.",
        "previous_body": "{{ ageing_count }} school(s) have been onboarding for more than {{ stale_after_days }} days, and {{ expired_count }} were suspended in the last {{ window_days }} days.",
    },
    "user.account_locked": {
        "subject": "Your account is locked{% if locked_at %} since {{ locked_at }}{% endif %}",
        "body": "Repeated failed sign-in attempts caused it. An administrator has to restore access.",
        "previous_body": "Account locked: Your account was locked on {{ locked_at }} due to repeated failed login attempts.",
    },
    "import.completed": {
        "subject": "{{ import_type }}: {{ success_count }} record{{ success_count|pluralize }} imported",
        "body": "{{ error_count }} row{{ error_count|pluralize }} had errors. Open the batch to review them.",
        "previous_body": "Import complete: {{ import_type }} - {{ success_count }} records imported successfully. Errors: {{ error_count }}.",
    },
    "import.failed": {
        "subject": "{{ import_type }} import failed",
        "body": "{% if error_summary %}{{ error_summary }} {% endif %}Fix the file and upload it again.",
        "previous_body": "Import failed: {{ import_type }} could not be completed. {{ error_summary }}",
    },
    "task.completed": {
        "subject": "{{ label }} finished successfully",
        "body": "No errors were reported.",
        "previous_body": "{{ label }} finished successfully.",
    },
    "task.failed": {
        "subject": "{{ label }} did not finish",
        "body": "{% if error %}{{ error }}{% else %}No error detail was recorded.{% endif %}",
        "previous_body": "{{ label }} did not finish. {{ error }}",
    },
    "export.run_completed": {
        "subject": "{{ export_name }} export is ready",
        "body": "{{ rows }} row{{ rows|pluralize }}. Reference {{ reference }}.{% if error %} {{ error }}{% endif %}",
        "previous_body": "{{ export_name }} is ready - {{ rows }} rows. {{ error }}",
    },
    "export.run_failed": {
        "subject": "{{ export_name }} export failed",
        "body": "{% if error %}{{ error }} {% endif %}Reference {{ reference }}. Open the run to see the full record.",
        "previous_body": "{{ export_name }} failed to run. {{ error }}",
    },
    "todo.task_completed": {
        "subject": "\"{{ task_title }}\" is ready for your review",
        "body": "{{ assignee_name }} marked it done on {{ task_completed }}. Review it under Tasks → My Team.",
        "previous_body": "{{ assignee_name }} marked \"{{ task_title }}\" as done. Kindly review it under Tasks → My Team.",
    },
}


# Decide what one stored template should take from its new default.
def plan_changes(stored_subject, stored_body, definition):
    """Return the fields to write for one template, as ``{field: value}``.

    Two different rules, because the two fields say different things about
    whoever last touched them. A blank subject is the platform default, so
    filling it takes nothing away; any other subject was typed by a person and
    is left exactly as written. A body carries copy from the start, so the only
    safe signal that nobody has edited it is that it still matches the default
    it shipped with, byte for byte.
    """
    changes = {}
    if not (stored_subject or "").strip():
        changes["subject"] = definition["subject"]
    if stored_body == definition["previous_body"]:
        changes["body"] = definition["body"]
    return changes


def give_in_app_templates_a_subject(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")

    for event_key, definition in TEMPLATES.items():
        template = Template.objects.filter(
            event_type__key=event_key, channel="in_app",
        ).first()
        if template is None:
            continue

        changes = plan_changes(template.subject, template.body, definition)
        if not changes:
            continue
        for field, value in changes.items():
            setattr(template, field, value)
        template.save(update_fields=[*changes, "updated_at"])


def noop(apps, schema_editor):
    """The previous platform-maintained copy is not user-authored data."""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0017_refine_procurement_vendor_emails"),
    ]

    operations = [
        migrations.RunPython(give_in_app_templates_a_subject, noop),
    ]
