from django.db import migrations


SUBJECT = "Purchase order {{ po_number }} from {{ issuer_name }}"
BODY = (
    "Hello {{ vendor_name }},\n\n{{ issuer_name }} has issued purchase order {{ po_number }} "
    "to you. A PDF copy is attached.\n\nOrder date: {{ order_date }}\n"
    "Expected date: {{ expected_date }}\nTotal: {{ total }}\n"
    "Delivery address: {{ delivery_address }}\nPayment terms: {{ payment_terms }}\n\n"
    "{{ buyer_message }}\n\nContact: {{ buyer_name }} {{ buyer_email }}"
)
HTML = (
    "<!doctype html><html><body style=\"margin:0;background:#f4f7fb;font-family:Arial,sans-serif;color:#1d2939\">"
    "<table role=\"presentation\" width=\"100%\"><tr><td style=\"padding:28px 12px\">"
    "<table role=\"presentation\" width=\"620\" align=\"center\" style=\"max-width:100%;background:#fff;border:1px solid #d5dfea;border-radius:10px\">"
    "<tr><td style=\"padding:24px 28px;background:#0f2747;color:#fff\"><div>{{ issuer_name|escape }}</div><h1 style=\"margin:5px 0 0;font-size:24px\">Purchase order {{ po_number|escape }}</h1></td></tr>"
    "<tr><td style=\"padding:28px\"><p>Hello {{ vendor_name|escape }},</p><p>{{ issuer_name|escape }} has issued this approved purchase order to you. The business copy is attached as a PDF.</p>"
    "<table role=\"presentation\" width=\"100%\" cellpadding=\"8\" style=\"margin:20px 0;background:#f8fafc\">"
    "<tr><td><b>Order date</b><br>{{ order_date|escape }}</td><td><b>Expected date</b><br>{{ expected_date|escape }}</td></tr>"
    "<tr><td><b>Total</b><br>{{ total|escape }}</td><td><b>Payment terms</b><br>{{ payment_terms|escape }}</td></tr>"
    "<tr><td colspan=\"2\"><b>Delivery address</b><br>{{ delivery_address|linebreaksbr }}</td></tr></table>"
    "{% if buyer_message %}<div style=\"padding:14px 16px;border-left:4px solid #2f6fed;background:#f4f7fb\">{{ buyer_message|linebreaksbr }}</div>{% endif %}"
    "<p style=\"font-size:13px;color:#667085\">Questions? Contact {{ buyer_name|escape }}{% if buyer_email %} at {{ buyer_email|escape }}{% endif %}.</p>"
    "</td></tr></table></td></tr></table></body></html>"
)


def seed_purchase_order_email(apps, schema_editor):
    Event = apps.get_model("vs_notifications", "NotificationEventType")
    Template = apps.get_model("vs_notifications", "NotificationTemplate")
    event, _ = Event.objects.update_or_create(
        key="procurement.purchase_order_issued",
        defaults={
            "label": "Vendor purchase order",
            "description": "Sends a fully approved purchase order and its PDF to a vendor.",
            "source_module": "vs_procurement",
            "supported_channels": ["email"],
            "default_enabled": True,
            "is_transactional": True,
            "is_active": True,
        },
    )
    Template.objects.get_or_create(
        event_type=event,
        channel="email",
        defaults={"subject": SUBJECT, "body": BODY, "html_body": HTML, "is_active": True},
    )


def remove_purchase_order_email(apps, schema_editor):
    Event = apps.get_model("vs_notifications", "NotificationEventType")
    # Preserve sent-notification history and its protected event reference on rollback.
    Event.objects.filter(key="procurement.purchase_order_issued").update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("vs_notifications", "0003_dynamic_workflow_approval_subject")]
    operations = [migrations.RunPython(seed_purchase_order_email, remove_purchase_order_email)]
