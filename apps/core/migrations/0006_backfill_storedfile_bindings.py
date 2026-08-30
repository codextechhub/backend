"""Bind the files that already exist to the records they belong to.

Migration 0005 gave ``StoredFile`` the columns that make an authorised read
possible; without this one every row written before that point is unbound, and
an unbound row is refused. On a deployment with live media that is not a
tightening, it is an outage: every school logo, every expense receipt and every
vendor attachment already uploaded would stop loading at once.

So this walks the other way round - from each record that owns a file to the row
holding its bytes - because that is the only direction in which the answer
exists. A ``StoredFile`` cannot say whose it is; a ``SchoolBranding`` can say
which file is its logo, and which school it belongs to.

Three things it deliberately does not do:

* **It does not guess.** Where a path to the tenant runs through a null foreign
  key, the row is left unbound rather than assigned to a plausible tenant. An
  unbound row is refused, which is the safe failure; a wrongly bound one is
  served to the wrong school, which is the unsafe one.
* **It does not touch orphans.** A ``StoredFile`` no record points at any more
  stays unbound and unreadable. Those are the files the old model could never
  clean up, and they should not come back to life here.
* **It does not reverse.** Unbinding on the way down would discard bindings
  written by normal traffic since. Reversing 0005 drops the columns anyway.
"""
from django.db import migrations

#: ``(app_label, model, file_field, tenant_lookup)``.
#:
#: The lookup is an ORM path from the owning row to its tenant. It is written out
#: per model rather than derived, because each one reaches its customer by a
#: different route, and a wrong route here does not fail - it binds a file to the
#: wrong school.
BINDINGS = [
    # A school's crest belongs to the school.
    ("vs_schools", "SchoolBranding", "logo", "school__tenant"),
    # A CodeX staff photo belongs to the tenant the person is on.
    ("vs_user", "PlatformStaffProfile", "profile_photo", "user__tenant"),
    # Finance and procurement documents reach the customer through their entity.
    ("vs_finance", "ExpenseClaimLine", "receipt", "claim__entity__tenant"),
    ("vs_finance", "FinanceDocumentDelivery", "pdf_file", "entity__tenant"),
    ("vs_procurement", "VendorQuotationAttachment", "file",
     "quotation__rfq__entity__tenant"),
    ("vs_procurement", "VendorInvoiceAttachment", "file",
     "vendor_invoice__entity__tenant"),
    ("vs_procurement", "VendorPaymentAttachment", "file", "payment__entity__tenant"),
    ("vs_procurement", "PurchaseOrderVendorDelivery", "pdf_file",
     "purchase_order__entity__tenant"),
    # These two carry the tenant themselves.
    ("vs_import_data", "ImportBatch", "file", "tenant"),
    ("vs_tickets", "TicketAttachment", "file", "ticket__tenant"),
]

#: File fields added after this migration ran.
#:
#: A binding cannot simply be appended to :data:`BINDINGS` above: that list is
#: resolved against THIS migration's own historical project state, where a
#: later app does not exist, and a model missing from that state is skipped by
#: the loop rather than backfilled - which is the silent failure the whole
#: exercise exists to prevent.
#:
#: So a later field is declared here instead, and its own app carries the
#: migration that runs the backfill for it. The exhaustiveness test reads the
#: union, so a FileField still cannot be added without somebody deciding which
#: of the two lists it belongs in.
LATER_BINDINGS = [
    # M11. Both carry their own tenant, and both models are created in the
    # release that introduced the binding, so there is nothing to rescue - the
    # backfill is a no-op by construction and is run anyway, because "there
    # cannot be any rows" is exactly the assumption that turns out to be wrong.
    ("vs_students", "Student", "photo", "tenant"),
    ("vs_students", "StudentDocument", "file", "tenant"),
]


def backfill(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    StoredFile = apps.get_model("core", "StoredFile")

    bound = 0
    skipped_no_tenant = 0

    for app_label, model_name, field_name, tenant_lookup in BINDINGS:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:  # pragma: no cover - model gone from a future tree
            continue

        # get_for_model records the same (app_label, model) pair the live
        # registry resolves, which is what ``core.media.authorize`` reads back.
        # It creates the row if the post_migrate hook has not run yet.
        content_type = ContentType.objects.get_for_model(model)

        # values_list, not instances: the ORM hands back the stored *name* here,
        # where attribute access would hand back a FieldFile, and it fetches the
        # tenant down the same join instead of one query per row.
        rows = (
            model.objects
            .exclude(**{field_name: ""})
            .exclude(**{f"{field_name}__isnull": True})
            .values_list("pk", field_name, tenant_lookup)
            .iterator(chunk_size=1000)
        )
        for pk, name, tenant_id in rows:
            if not name:
                continue
            if tenant_id is None:
                # A null anywhere along the join. Leave it unbound: refused is
                # the safe failure, bound to a guess is the unsafe one.
                skipped_no_tenant += 1
                continue
            bound += StoredFile.objects.filter(name=name).update(
                tenant_id=tenant_id,
                owner_content_type=content_type,
                owner_object_id=str(pk),
                owner_field=field_name,
            )

    orphaned = StoredFile.objects.filter(owner_content_type__isnull=True).count()
    if bound or skipped_no_tenant or orphaned:
        print(
            f"\n  core.StoredFile backfill: {bound} bound, "
            f"{skipped_no_tenant} skipped (no tenant on the owning record), "
            f"{orphaned} left unbound and unreadable (no record points at them)."
        )


class Migration(migrations.Migration):

    # Pinned to each app's latest migration at the time of writing, not to its
    # initial one. Several of these models (the two delivery tables especially)
    # arrived long after 0001, and depending on 0001 would hand this migration a
    # historical registry that simply does not contain them - which
    # ``apps.get_model`` reports as LookupError and this file, correctly, skips.
    # A backfill that silently covers half the tables is worse than none.
    #
    # No migration in any of these apps depends on ``core``, so pinning forward
    # like this introduces no cycle.
    dependencies = [
        ("core", "0005_storedfile_created_by_storedfile_owner_content_type_and_more"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("vs_schools", "0009_remove_branchprimaryadmin_role_label_and_more"),
        ("vs_user", "0010_request_bound_action_tokens"),
        ("vs_finance", "0024_employeesalary_branch"),
        ("vs_procurement", "0033_vendor_bank_code"),
        ("vs_import_data", "0008_alter_importbatch_status_alter_importjob_status"),
        ("vs_tickets", "0007_guideanalyticsevent"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
