"""Refresh the seven standard procurement vendor email templates.

Only platform-maintained templates are changed. Staff-authored subject lines,
messages, actions, and HTML remain exactly as written.
"""

from django.db import migrations


TEMPLATES = {
    "procurement.purchase_order_issued": {
        "subject": "Purchase order {{ po_number }} from {{ issuer_name }}",
        "body": (
            "Hello {{ vendor_name }},\n\n"
            "{{ issuer_name }} has issued purchase order {{ po_number }} to you.\n\n"
            "PURCHASE ORDER DETAILS\n"
            "Purchase order: {{ po_number }}\n"
            "Order date: {{ order_date }}\n"
            "Expected delivery: {{ expected_date }}\n"
            "Total: {{ total }}\n"
            "Delivery address: {{ delivery_address }}\n"
            "Payment terms: {{ payment_terms }}\n"
            "{% if buyer_message %}\nNOTE FROM THE BUYER\n{{ buyer_message }}\n{% endif %}"
            "\nA PDF copy of the purchase order is attached. Review the document before "
            "fulfilling the order, and contact {{ buyer_name }}"
            "{% if buyer_email %} at {{ buyer_email }}{% endif %} if anything needs clarification."
        ),
        "cta_label": "",
        "cta_url": "",
    },
    "procurement.rfq_invitation": {
        "subject": "Quotation request {{ rfq_number }} from {{ issuer_name }}",
        "body": (
            "Hello {{ recipient_name }},\n\n"
            "{{ issuer_name }} has invited {{ vendor_name }} to submit a quotation.\n\n"
            "REQUEST DETAILS\n"
            "RFQ: {{ rfq_number }}\n"
            "Title: {{ rfq_title }}\n"
            "Response deadline: {{ deadline }}\n\n"
            "Open the secure quotation form to review the requested items, submit a "
            "quotation, or decline the request. You will verify your email address with "
            "a one-time code before you can edit a response.\n\n"
            "{{ invitation_url }}\n\n"
            "If you were not expecting this request, do not forward the link. Contact "
            "{{ issuer_name }} to confirm it."
        ),
        "cta_label": "Review quotation request",
        "cta_url": "{{ invitation_url }}",
    },
    "procurement.rfq_verification_code": {
        "subject": "Your verification code for {{ rfq_number }}",
        "body": (
            "Hello {{ recipient_name }},\n\n"
            "Use this one-time code to verify access to the secure quotation form.\n\n"
            "VERIFICATION CODE\n"
            "{{ verification_code }}\n\n"
            "REQUEST DETAILS\n"
            "RFQ: {{ rfq_number }}\n"
            "Issued by: {{ issuer_name }}\n"
            "Vendor: {{ vendor_name }}\n"
            "Code expires in: {{ expiry_minutes }} minutes\n\n"
            "Enter the code in the quotation form you already opened. Do not share it. "
            "If you did not request this code, you can ignore this email."
        ),
        "cta_label": "",
        "cta_url": "",
    },
    "procurement.rfq_reminder": {
        "subject": "Reminder: quotation due for {{ rfq_number }}",
        "body": (
            "Hello {{ recipient_name }},\n\n"
            "{{ vendor_name }} has not yet submitted or declined the quotation request "
            "from {{ issuer_name }}.\n\n"
            "REQUEST DETAILS\n"
            "RFQ: {{ rfq_number }}\n"
            "Title: {{ rfq_title }}\n"
            "Response deadline: {{ deadline }}\n\n"
            "Open the secure quotation form to finish your response or decline the "
            "request before the deadline.\n\n"
            "{{ invitation_url }}"
        ),
        "cta_label": "Complete your response",
        "cta_url": "{{ invitation_url }}",
    },
    "procurement.quotation_receipt": {
        "subject": "Quotation {{ quotation_number }} received for {{ rfq_number }}",
        "body": (
            "Hello {{ recipient_name }},\n\n"
            "{{ issuer_name }} received {{ vendor_name }}'s quotation.\n\n"
            "SUBMISSION DETAILS\n"
            "Quotation: {{ quotation_number }}\n"
            "RFQ: {{ rfq_number }}\n"
            "Revision: {{ revision }}\n"
            "Submitted at: {{ submitted_at }}\n\n"
            "This confirms receipt only. It does not mean the quotation has been accepted "
            "or that an order has been awarded.\n\n"
            "Open the secure form to view the submitted quotation and its read-only receipt.\n\n"
            "{{ invitation_url }}"
        ),
        "cta_label": "View submitted quotation",
        "cta_url": "{{ invitation_url }}",
    },
    "procurement.rfq_amended": {
        "subject": (
            "{% if response_required == 'Yes' %}Action required: {% endif %}"
            "{{ rfq_number }} was amended"
        ),
        "body": (
            "Hello {{ recipient_name }},\n\n"
            "{{ issuer_name }} amended the quotation request sent to {{ vendor_name }}.\n\n"
            "AMENDMENT DETAILS\n"
            "RFQ: {{ rfq_number }}\n"
            "Title: {{ rfq_title }}\n"
            "Version: {{ rfq_version }}\n"
            "Summary: {{ amendment_summary }}\n"
            "Response required: {{ response_required }}\n"
            "Current deadline: {{ deadline }}\n\n"
            "{% if response_required == 'Yes' %}Open the secure quotation form, "
            "acknowledge the amendment, and submit an updated response before the deadline. "
            "If you had already submitted, your quotation has been returned to draft."
            "{% else %}No new response is required. Open the secure quotation form to review "
            "the change and keep a copy with your records.{% endif %}\n\n"
            "{{ invitation_url }}"
        ),
        "cta_label": "Review the amendment",
        "cta_url": "{{ invitation_url }}",
    },
    "procurement.rfq_deadline_extended": {
        "subject": "Quotation deadline extended for {{ rfq_number }}",
        "body": (
            "Hello {{ recipient_name }},\n\n"
            "{{ issuer_name }} extended the response deadline for {{ vendor_name }}.\n\n"
            "UPDATED REQUEST DETAILS\n"
            "RFQ: {{ rfq_number }}\n"
            "Title: {{ rfq_title }}\n"
            "New response deadline: {{ deadline }}\n\n"
            "Your existing secure link remains valid. Open the quotation form to submit, "
            "revise, or decline your response before the new deadline.\n\n"
            "{{ invitation_url }}"
        ),
        "cta_label": "Open the quotation form",
        "cta_url": "{{ invitation_url }}",
    },
}


def refine_standard_procurement_emails(apps, schema_editor):
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    from vs_notifications.services.layout import (
        EMAIL_BRAND_LOGO_PLACEHOLDER,
        EMAIL_BRAND_PLACEHOLDER,
        compose_email_html,
    )

    for event_key, definition in TEMPLATES.items():
        template = Template.objects.filter(
            event_type__key=event_key,
            channel="email",
            html_is_custom=False,
        ).first()
        if template is None:
            continue

        template.subject = definition["subject"]
        template.body = definition["body"]
        template.cta_label = definition["cta_label"]
        template.cta_url = definition["cta_url"]
        template.html_body = compose_email_html(
            subject=definition["subject"],
            body=definition["body"],
            cta_label=definition["cta_label"],
            cta_url=definition["cta_url"],
            brand=EMAIL_BRAND_PLACEHOLDER,
            brand_logo_url=EMAIL_BRAND_LOGO_PLACEHOLDER,
            as_template=True,
        )
        template.save(
            update_fields=[
                "subject",
                "body",
                "cta_label",
                "cta_url",
                "html_body",
            ]
        )


def noop(apps, schema_editor):
    """The previous platform-maintained copy is not user-authored data."""


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0016_refine_onboarding_emails"),
    ]

    operations = [
        migrations.RunPython(refine_standard_procurement_emails, noop),
    ]
