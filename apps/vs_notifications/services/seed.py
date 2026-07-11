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


# Seed the authoritative event catalogue without deleting retired keys.
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
        # Upsert by stable key so labels/defaults can evolve without duplicate rows.
        _, created = NotificationEventType.objects.update_or_create(
            key=entry["key"],
            defaults={
                "label":              entry["label"],
                "description":        entry.get("description", ""),
                "source_module":      entry["source_module"],
                "supported_channels": entry["supported_channels"],
                "default_enabled":    entry.get("default_enabled", True),
                "is_transactional":   entry.get("is_transactional", False),
                # Registry-driven: events stay inactive until a module emits them.
                "is_active":          entry.get("is_active", True),
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


# Materialize platform defaults for non-transactional event/channel pairs.
def seed_platform_settings() -> dict:
    """
    Create the platform-wide (school=NULL) NotificationSetting rows.

    For each active NotificationEventType and each of its supported_channels a
    platform row is created with is_enabled = event_type.default_enabled.
    These are the defaults resolve_channels() falls back to before hitting the
    event type's default_enabled directly; seeding them makes the defaults
    explicit and admin-visible.

    Transactional event types are skipped — they bypass settings entirely, so a
    setting row for them would be dead data.

    Uses get_or_create — existing rows are never overwritten, so an admin's
    platform-level change is preserved and new event types are picked up on the
    next run.

    Returns:
        {"created": N, "skipped": N}
    """
    from ..models import NotificationEventType, NotificationSetting

    active_event_types = NotificationEventType.objects.filter(
        is_active=True, is_transactional=False,
    )

    created_count = 0
    skipped_count = 0

    for event_type in active_event_types:
        for channel in event_type.supported_channels:
            # Preserve admin changes; only missing platform rows are inserted.
            _, created = NotificationSetting.all_objects.get_or_create(
                school=None,
                event_type=event_type,
                channel=channel,
                defaults={"is_enabled": event_type.default_enabled},
            )
            if created:
                created_count += 1
            else:
                skipped_count += 1

    logger.info(
        "seed_platform_settings complete — created: %d, skipped: %d",
        created_count, skipped_count,
    )
    return {"created": created_count, "skipped": skipped_count}


# Materialize school-specific override rows for one school.
def seed_school_settings(school) -> dict:
    """
    Create school-scoped NotificationSetting override rows for one school.

    This is an OPTIONAL explicit-override path (platform defaults, seeded by
    seed_platform_settings, already cover every school). It exists so a school
    can be pre-populated with concrete rows an admin can then toggle, and is
    still used by callers that want per-school rows materialised up front.

    For each active, non-transactional NotificationEventType and each supported
    channel, a row is created with is_enabled = event_type.default_enabled.
    Uses get_or_create — existing admin-configured rows are never overwritten.

    Args:
        school:  A School model instance.

    Returns:
        {"created": N, "skipped": N}
    """
    from ..models import NotificationEventType, NotificationSetting

    active_event_types = NotificationEventType.objects.filter(
        is_active=True, is_transactional=False,
    )

    created_count = 0
    skipped_count = 0

    for event_type in active_event_types:
        for channel in event_type.supported_channels:
            # Preserve school admin changes; onboarding only fills missing rows.
            _, created = NotificationSetting.all_objects.get_or_create(
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


# Seed default templates without overwriting Vision Staff customizations.
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
                # Missing defaults are non-fatal so one absent template does not block all seeding.
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
# Ported HTML email bodies (transactional events)
#
# Faithful ports of vs_user/templates/vs_user/emails/*.html with the nested
# Django-template variables ({{ user.first_name }} etc.) flattened to the flat
# context keys the render engine receives. Copy is preserved; only variable
# references and a couple of account-detail fields (which the flat context does
# not carry) were adjusted.
# ---------------------------------------------------------------------------

_INVITATION_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Welcome to {{ school_name }}</title>
  </head>
  <body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5;">
    <table role="presentation" style="width: 100%; border-collapse: collapse">
      <tr>
        <td align="center" style="padding: 40px 0">
          <table role="presentation" style="width: 600px; max-width: 100%; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);">
            <!-- Header -->
            <tr>
              <td style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 40px 30px; text-align: center; border-radius: 8px 8px 0 0;">
                <h1 style="margin: 0; color: #ffffff; font-size: 28px; font-weight: 600;">Welcome to {{ school_name }}</h1>
              </td>
            </tr>
            <!-- Body -->
            <tr>
              <td style="padding: 40px 30px">
                <p style="margin: 0 0 20px; color: #333333; font-size: 16px; line-height: 1.6;">Hello <strong>{{ user_first_name }}</strong>,</p>
                <p style="margin: 0 0 20px; color: #333333; font-size: 16px; line-height: 1.6;">You have been invited to join <strong>{{ school_name }}</strong> on XVision System. To get started, please activate your account by setting up your password.</p>
                <!-- Account Details Box -->
                <div style="background-color: #f8f9fa; border-left: 4px solid #667eea; padding: 20px; margin: 30px 0; border-radius: 4px;">
                  <p style="margin: 0 0 10px; color: #666666; font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Your Account Details</p>
                  <p style="margin: 0 0 8px; color: #333333; font-size: 15px;"><strong>Name:</strong> {{ user_full_name }}</p>
                  <p style="margin: 0; color: #333333; font-size: 15px;"><strong>Institution:</strong> {{ school_name }}</p>
                </div>
                <!-- CTA Button -->
                <table role="presentation" style="margin: 30px 0">
                  <tr>
                    <td align="center">
                      <a href="{{ invitation_url }}" style="display: inline-block; padding: 16px 40px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #ffffff; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 16px; box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);">Activate Your Account</a>
                    </td>
                  </tr>
                </table>
                <p style="margin: 30px 0 20px; color: #666666; font-size: 14px; line-height: 1.6;">Or copy and paste this URL into your browser:</p>
                <p style="margin: 0 0 30px; padding: 15px; background-color: #f8f9fa; border-radius: 4px; word-break: break-all; font-size: 13px; color: #667eea; font-family: 'Courier New', monospace;">{{ invitation_url }}</p>
                <!-- Important Notice -->
                <div style="background-color: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin: 30px 0; border-radius: 4px;">
                  <p style="margin: 0; color: #856404; font-size: 14px; line-height: 1.6;"><strong>⏰ Important:</strong> This invitation link will expire in <strong>{{ expiry_days }} days</strong>. Please activate your account before then.</p>
                </div>
                <p style="margin: 30px 0 0; color: #666666; font-size: 14px; line-height: 1.6;">If you didn't expect this invitation or have any questions, please contact your administrator.</p>
              </td>
            </tr>
            <!-- Footer -->
            <tr>
              <td style="background-color: #f8f9fa; padding: 30px; text-align: center; border-radius: 0 0 8px 8px; border-top: 1px solid #e9ecef;">
                <p style="margin: 0 0 10px; color: #999999; font-size: 13px">XVision System</p>
                <p style="margin: 0; color: #999999; font-size: 13px">Powering Smart Institutions</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


_PASSWORD_RESET_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Reset Your Password</title>
  </head>
  <body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5;">
    <table role="presentation" style="width: 100%; border-collapse: collapse">
      <tr>
        <td align="center" style="padding: 40px 0">
          <table role="presentation" style="width: 600px; max-width: 100%; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);">
            <!-- Header -->
            <tr>
              <td style="{% if origin == 'ADMIN' %}background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);{% else %}background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);{% endif %} padding: 40px 30px; text-align: center; border-radius: 8px 8px 0 0;">
                <h1 style="margin: 0; color: #ffffff; font-size: 28px; font-weight: 600;">{% if origin == 'ADMIN' %}Password Reset Requested{% else %}Reset Your Password{% endif %}</h1>
              </td>
            </tr>
            <!-- Body -->
            <tr>
              <td style="padding: 40px 30px">
                <p style="margin: 0 0 20px; color: #333333; font-size: 16px; line-height: 1.6;">Hello <strong>{{ user_first_name }}</strong>,</p>
                {% if origin == 'ADMIN' %}
                <p style="margin: 0 0 20px; color: #333333; font-size: 16px; line-height: 1.6;">Your administrator has initiated a password reset for your CodeX Vision account. Use the link below to create a new password.</p>
                {% else %}
                <p style="margin: 0 0 20px; color: #333333; font-size: 16px; line-height: 1.6;">We received a request to reset the password for your CodeX Vision account. Click the button below to create a new password.</p>
                {% endif %}
                <!-- CTA Button -->
                <table role="presentation" style="margin: 30px 0">
                  <tr>
                    <td align="center">
                      <a href="{{ reset_url }}" style="display: inline-block; padding: 16px 40px; {% if origin == 'ADMIN' %}background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);{% else %}background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);{% endif %} color: #ffffff; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 16px; {% if origin == 'ADMIN' %}box-shadow: 0 4px 12px rgba(240, 147, 251, 0.4);{% else %}box-shadow: 0 4px 12px rgba(79, 172, 254, 0.4);{% endif %}">Reset Password</a>
                    </td>
                  </tr>
                </table>
                <p style="margin: 30px 0 20px; color: #666666; font-size: 14px; line-height: 1.6;">Or copy and paste this URL into your browser:</p>
                <p style="margin: 0 0 30px; padding: 15px; background-color: #f8f9fa; border-radius: 4px; word-break: break-all; font-size: 13px; {% if origin == 'ADMIN' %}color: #f5576c;{% else %}color: #4facfe;{% endif %} font-family: 'Courier New', monospace;">{{ reset_url }}</p>
                <!-- Expiry Notice -->
                <div style="{% if origin == 'ADMIN' %}background-color: #fff0f6; border-left: 4px solid #f5576c;{% else %}background-color: #e7f7ff; border-left: 4px solid #4facfe;{% endif %} padding: 15px; margin: 30px 0; border-radius: 4px;">
                  <p style="margin: 0; {% if origin == 'ADMIN' %}color: #c41d3a;{% else %}color: #0066cc;{% endif %} font-size: 14px; line-height: 1.6;"><strong>⏰ This link will expire in {{ expiry_hours }} hour{% if expiry_hours > 1 %}s{% endif %}.</strong> After that, you'll need to request a new password reset.</p>
                </div>
                <!-- Security Notice -->
                <div style="background-color: #f8f9fa; padding: 20px; margin: 30px 0; border-radius: 4px;">
                  <p style="margin: 0 0 10px; color: #666666; font-size: 14px; font-weight: 600;">🔒 Security Notice</p>
                  <p style="margin: 0; color: #666666; font-size: 14px; line-height: 1.6;">If you didn't request this password reset, please ignore this email. Your password will remain unchanged. For security concerns, contact your administrator immediately.</p>
                </div>
              </td>
            </tr>
            <!-- Footer -->
            <tr>
              <td style="background-color: #f8f9fa; padding: 30px; text-align: center; border-radius: 0 0 8px 8px; border-top: 1px solid #e9ecef;">
                <p style="margin: 0 0 10px; color: #999999; font-size: 13px">XVision System</p>
                <p style="margin: 0; color: #999999; font-size: 13px">Empowering education through technology</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Default template content
# ---------------------------------------------------------------------------

# Build default copy for every seeded event/channel template.
def _build_default_templates() -> dict:
    """
    Returns a dict keyed by (event_key, channel) with subject and body defaults.

    All available context variables are referenced so Vision Staff can see
    the full variable set when they customise.
    """
    from ..constants import ChannelChoices as C

    return {

        # ── support tickets ─────────────────────────────────────────────────
        ("ticket.created", C.IN_APP): {
            "subject": "",
            "body": "New ticket {{ ticket_number }}: {{ ticket_title }} was created by {{ requester_name }}.",
        },
        ("ticket.created", C.EMAIL): {
            "subject": "New support ticket {{ ticket_number }} — {{ ticket_title }}",
            "body": (
                "A new support ticket has been created.\n\n"
                "Ticket: {{ ticket_number }}\n"
                "Title: {{ ticket_title }}\n"
                "Category: {{ ticket_category }}\n"
                "Priority: {{ ticket_priority }}\n"
                "Requester: {{ requester_name }}\n\n"
                "Please log in to CodeX Vision to review it."
            ),
        },
        ("ticket.assigned", C.IN_APP): {
            "subject": "",
            "body": "Ticket {{ ticket_number }} has been assigned to you.",
        },
        ("ticket.assigned", C.EMAIL): {
            "subject": "Ticket assigned to you — {{ ticket_number }}",
            "body": (
                "A support ticket has been assigned to you.\n\n"
                "Ticket: {{ ticket_number }}\n"
                "Title: {{ ticket_title }}\n"
                "Priority: {{ ticket_priority }}\n"
                "Requester: {{ requester_name }}\n"
            ),
        },
        ("ticket.status_changed", C.IN_APP): {
            "subject": "",
            "body": "Ticket {{ ticket_number }} moved from {{ old_status }} to {{ new_status }}.",
        },
        ("ticket.status_changed", C.EMAIL): {
            "subject": "Ticket status updated — {{ ticket_number }}",
            "body": "Ticket {{ ticket_number }} ({{ ticket_title }}) moved from {{ old_status }} to {{ new_status }}.",
        },
        ("ticket.commented", C.IN_APP): {
            "subject": "",
            "body": "{{ actor_name }} commented on ticket {{ ticket_number }}: {{ comment_body }}",
        },
        ("ticket.commented", C.EMAIL): {
            "subject": "New comment on ticket {{ ticket_number }}",
            "body": (
                "{{ actor_name }} added a comment to ticket {{ ticket_number }}.\n\n"
                "Title: {{ ticket_title }}\n"
                "Comment: {{ comment_body }}"
            ),
        },
        ("ticket.resolved", C.IN_APP): {
            "subject": "",
            "body": "Ticket {{ ticket_number }} has been resolved.",
        },
        ("ticket.resolved", C.EMAIL): {
            "subject": "Ticket resolved — {{ ticket_number }}",
            "body": "Ticket {{ ticket_number }} ({{ ticket_title }}) has been resolved.",
        },
        ("ticket.closed", C.IN_APP): {
            "subject": "",
            "body": "Ticket {{ ticket_number }} has been closed.",
        },
        ("ticket.closed", C.EMAIL): {
            "subject": "Ticket closed — {{ ticket_number }}",
            "body": "Ticket {{ ticket_number }} ({{ ticket_title }}) has been closed.",
        },
        ("ticket.reopened", C.IN_APP): {
            "subject": "",
            "body": "Ticket {{ ticket_number }} has been reopened.",
        },
        ("ticket.reopened", C.EMAIL): {
            "subject": "Ticket reopened — {{ ticket_number }}",
            "body": "Ticket {{ ticket_number }} ({{ ticket_title }}) has been reopened.",
        },
        ("ticket.attachment_added", C.IN_APP): {
            "subject": "",
            "body": "{{ actor_name }} attached {{ attachment_name }} to ticket {{ ticket_number }}.",
        },
        ("ticket.attachment_added", C.EMAIL): {
            "subject": "Attachment added to ticket {{ ticket_number }}",
            "body": "{{ actor_name }} attached {{ attachment_name }} to ticket {{ ticket_number }} ({{ ticket_title }}).",
        },

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

        # ── workflow.stage_activated ────────────────────────────────────────
        ("workflow.stage_activated", C.IN_APP): {
            "subject": "",
            "body": (
                "Approval required: {{ document_title }} submitted by "
                "{{ submitter_name }} is awaiting your decision at stage '{{ stage_name }}'."
            ),
        },
        ("workflow.stage_activated", C.EMAIL): {
            "subject": "Action required: {{ document_type }} awaiting your approval",
            "body": (
                "A document is awaiting your approval.\n\n"
                "Document type: {{ document_type }}\n"
                "Title: {{ document_title }}\n"
                "Submitted by: {{ submitter_name }}\n"
                "Current stage: {{ stage_name }}\n\n"
                "Please log in to CodeX Vision to review and act on this request.\n\n"
                "CodeX Vision"
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
        # NB: billing.* events are fired by the domain-neutral vs_finance ledger,
        # which knows a generic {{ customer_name }} (the billing party) — not a
        # structured student first/last. Keep these on customer_name.
        ("billing.invoice_issued", C.IN_APP): {
            "subject": "",
            "body": (
                "New invoice: ₦{{ invoice_amount }} is due for "
                "{{ customer_name }} by {{ due_date }}."
            ),
        },
        ("billing.invoice_issued", C.EMAIL): {
            "subject": "New fee invoice — {{ customer_name }}",
            "body": (
                "Dear Parent/Guardian,\n\n"
                "A new invoice has been issued for your child's school fees.\n\n"
                "Bill to: {{ customer_name }}\n"
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
                "{{ customer_name }} on {{ payment_date }}."
            ),
        },
        ("billing.payment_received", C.EMAIL): {
            "subject": "Payment confirmed — {{ customer_name }}",
            "body": (
                "Dear Parent/Guardian,\n\n"
                "We have received your payment. Thank you.\n\n"
                "Bill to: {{ customer_name }}\n"
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
                "{{ customer_name }} — {{ days_overdue }} day(s) overdue."
                " {{ reminder_message }}"
            ),
        },
        ("billing.invoice_overdue", C.EMAIL): {
            "subject": "Overdue fee invoice — {{ customer_name }}",
            "body": (
                "Dear Parent/Guardian,\n\n"
                "This is a reminder that the following invoice is overdue.\n\n"
                "{{ reminder_message }}\n\n"
                "Bill to: {{ customer_name }}\n"
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
                "{{ customer_name }}."
            ),
        },
        ("billing.refund_processed", C.EMAIL): {
            "subject": "Refund processed — {{ customer_name }}",
            "body": (
                "Dear Parent/Guardian,\n\n"
                "A refund has been processed on your account.\n\n"
                "Bill to: {{ customer_name }}\n"
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

        # ── user.invited (EMAIL only, transactional) ────────────────────────
        # Ported from vs_user/templates/vs_user/emails/invitation.{txt,html}.
        # The Django-template variables there ({{ user.first_name }} etc.) are
        # flattened to the context keys the render engine receives:
        #   user_first_name, user_full_name, school_name, invitation_url, expiry_days
        ("user.invited", C.EMAIL): {
            "subject": (
                "{% if has_school %}You have been invited to {{ school_name }} "
                "on XVision System{% else %}You have been invited to XVision "
                "System{% endif %}"
            ),
            "body": (
                "Welcome to {{ school_name }}\n\n"
                "Hello {{ user_first_name }},\n\n"
                "You have been invited to join {{ school_name }} on XVision System.\n\n"
                "YOUR ACCOUNT DETAILS\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Name:        {{ user_full_name }}\n"
                "Institution: {{ school_name }}\n\n"
                "ACTIVATE YOUR ACCOUNT\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Click the link below to set up your password and activate your account:\n\n"
                "{{ invitation_url }}\n\n"
                "⏰ IMPORTANT: This invitation link will expire in {{ expiry_days }} days.\n\n"
                "If you didn't expect this invitation or have any questions, please contact "
                "your administrator.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "XVision System\n"
                "Powering Smart Institutions"
            ),
            "html_body": _INVITATION_HTML,
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

        # ── user.password_reset (EMAIL only, transactional) ─────────────────
        # Ported from vs_user/templates/vs_user/emails/password_reset.{txt,html}.
        # Flat context keys: user_first_name, reset_url, expiry_hours, origin,
        # sender_name.
        ("user.password_reset", C.EMAIL): {
            "subject": (
                "{% if origin == 'ADMIN' %}Your CodeX Vision password has been "
                "reset by an administrator{% else %}Reset your CodeX Vision "
                "password{% endif %}"
            ),
            "body": (
                "{% if origin == 'ADMIN' %}PASSWORD RESET REQUESTED"
                "{% else %}RESET YOUR PASSWORD{% endif %}\n\n"
                "Hello {{ user_first_name }},\n\n"
                "{% if origin == 'ADMIN' %}Your administrator has initiated a password "
                "reset for your CodeX Vision account.{% else %}We received a request to "
                "reset the password for your CodeX Vision account.{% endif %}\n\n"
                "RESET YOUR PASSWORD\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Click the link below to create a new password:\n\n"
                "{{ reset_url }}\n\n"
                "⏰ This link will expire in {{ expiry_hours }} hour"
                "{% if expiry_hours > 1 %}s{% endif %}.\n\n"
                "🔒 SECURITY NOTICE\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "If you didn't request this password reset, please ignore this email.\n"
                "Your password will remain unchanged.\n\n"
                "For security concerns, contact your administrator immediately.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "XVision System\n"
                "Empowering education through technology"
            ),
            "html_body": _PASSWORD_RESET_HTML,
        },

        # ── task.completed / task.failed (core background jobs, IN_APP) ──────
        ("task.completed", C.IN_APP): {
            "subject": "",
            "body": "Your background task '{{ label }}' finished successfully.",
        },
        ("task.failed", C.IN_APP): {
            "subject": "",
            "body": "Your background task '{{ label }}' FAILED. {{ error }}",
        },

        # ── todo.task_completed (review request) ────────────────────────────
        ("todo.task_completed", C.IN_APP): {
            "subject": "",
            "body": (
                "{{ assignee_name }} marked \"{{ task_title }}\" as done. "
                "Kindly review it under Tasks → My Team."
            ),
        },
        ("todo.task_completed", C.EMAIL): {
            "subject": "Review requested: \"{{ task_title }}\" marked as done",
            "body": (
                "Hello {{ reviewer_first }},\n\n"
                "{{ assignee_name }} has marked the task below as completed and it is\n"
                "awaiting your review.\n\n"
                "  Task       : {{ task_title }}\n"
                "{% if task_description %}  Details    : {{ task_description }}\n{% endif %}"
                "  Metric     : {{ task_metric }}\n"
                "  Target     : {{ task_target }}\n"
                "  Priority   : {{ task_priority }}\n"
                "  Deadline   : {{ task_deadline }}\n"
                "  Completed  : {{ task_completed }}\n"
                "{% if task_department %}  Department : {{ task_department }}\n{% endif %}"
                "\n"
                "Review it on the console under Tasks → My Team → {{ assignee_first }}.\n\n"
                "— CodeX Vision Console (automated message)"
            ),
        },
    }
