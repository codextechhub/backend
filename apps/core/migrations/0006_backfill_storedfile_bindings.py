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

#: File fields that arrive after this migration, keyed by the migration that
#: backfills each one: ``"app_label.migration_name" -> [binding, ...]``.
#:
#: A later binding cannot simply be appended to :data:`BINDINGS` above. That
#: list is resolved against THIS migration's own historical project state, in
#: which a later model does not exist, and a model missing from that state is
#: skipped by the loop rather than backfilled - the silent failure the whole
#: exercise exists to prevent.
#:
#: The key is what stops the same trap reappearing one level down. A field
#: added in an app's third migration is invisible to its second, so a single
#: app-wide list read by whichever of them runs first would hand that migration
#: a field its own project state has never heard of. Each wave therefore names
#: the migration that runs it, and that migration reads only its own key.
#:
#: The exhaustiveness test in ``core.tests`` reads the union of every value
#: here and :data:`BINDINGS`, so a FileField cannot be added anywhere in the
#: codebase without somebody deciding which wave it belongs to. A second test
#: resolves each wave against its own migration's historical state, which
#: catches what a run cannot: a field that state does not have raises, but a
#: model it has never heard of is skipped, so the migration reports success and
#: binds nothing.
LATER_BINDINGS = {
    # Student and StudentDocument arrive with the module itself, a guardian's
    # photograph one migration later. In both waves the column is created in
    # the same release as its binding, so there is nothing to rescue - the
    # backfill is a no-op by construction and runs anyway, because "there
    # cannot be any rows yet" is exactly the assumption that turns out to be
    # wrong. All three models carry their own tenant.
    "vs_students.0002_bind_student_files": [
        ("vs_students", "Student", "photo", "tenant"),
        ("vs_students", "StudentDocument", "file", "tenant"),
    ],
    "vs_students.0004_bind_guardian_photos": [
        ("vs_students", "Guardian", "photo", "tenant"),
    ],
}


def bind_rows(apps, bindings):
    """Stamp each file's ``StoredFile`` row with the record and tenant owning it.

    Returns ``(bound, skipped_no_tenant)``.

    Every wave runs through here - this migration's own list and each later one
    - so all of them treat a null tenant identically: the row is left unbound,
    because a refused file is the safe failure and a file bound to a guess is
    one served to the wrong school.

    ``bindings`` is resolved against the caller's own historical project state.
    A model that state has never heard of is skipped, which is safe only because
    each wave names the migration that owns it. A field the state does not have
    raises instead, and that is what makes a misfiled binding loud rather than
    quietly ineffective.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")
    StoredFile = apps.get_model("core", "StoredFile")

    bound = 0
    skipped_no_tenant = 0

    for app_label, model_name, field_name, tenant_lookup in bindings:
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

    return bound, skipped_no_tenant


def backfill(apps, schema_editor):
    StoredFile = apps.get_model("core", "StoredFile")

    bound, skipped_no_tenant = bind_rows(apps, BINDINGS)

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
