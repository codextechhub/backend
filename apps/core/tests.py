"""
Tests for the database-backed media storage (B9) and its serving view.
"""
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import StoredFile
from core.storage import DatabaseStorage


def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


class DatabaseStorageTests(TestCase):
    def setUp(self):
        self.storage = DatabaseStorage()

    def test_default_storage_is_database_backed(self):
        self.assertIsInstance(default_storage, DatabaseStorage)

    def test_save_open_roundtrip(self):
        name = self.storage.save("school_logos/logo.png", ContentFile(b"\x89PNG fake"))
        self.assertTrue(self.storage.exists(name))
        with self.storage.open(name) as fh:
            self.assertEqual(fh.read(), b"\x89PNG fake")
        row = StoredFile.objects.get(name=name)
        self.assertEqual(row.content_type, "image/png")
        self.assertEqual(row.size, 9)
        self.assertEqual(self.storage.url(name), f"/media/{name}")

    def test_csv_accepted_exe_rejected(self):
        self.storage.save("imports/students.csv", ContentFile(b"a,b,c"))
        with self.assertRaises(ValidationError):
            self.storage.save("evil/payload.exe", ContentFile(b"MZ"))
        with self.assertRaises(ValidationError):
            self.storage.save("evil/page.svg", ContentFile(b"<svg/>"))

    def test_size_ceiling_enforced(self):
        with self.settings(MEDIA_DB_MAX_BYTES=10):
            with self.assertRaises(ValidationError):
                self.storage.save("imports/big.csv", ContentFile(b"x" * 11))

    def test_delete(self):
        name = self.storage.save("imports/tmp.csv", ContentFile(b"1"))
        self.storage.delete(name)
        self.assertFalse(self.storage.exists(name))

    def test_path_traversal_blocked(self):
        from django.core.exceptions import SuspiciousOperation

        with self.assertRaises(SuspiciousOperation):
            self.storage.save("../../etc/passwd.csv", ContentFile(b"x"))

    def test_stored_name_is_unguessable(self):
        # The stored name keeps a readable prefix but carries a high-entropy token,
        # so a caller can't fetch a file by guessing a predictable path.
        name = self.storage.save("expense-receipts/receipt.pdf", ContentFile(b"%PDF fake"))
        self.assertNotEqual(name, "expense-receipts/receipt.pdf")
        self.assertTrue(name.startswith("expense-receipts/receipt-"))
        self.assertTrue(name.endswith(".pdf"))
        # Two uploads of the same filename get distinct, unguessable names.
        other = self.storage.save("expense-receipts/receipt.pdf", ContentFile(b"%PDF two"))
        self.assertNotEqual(name, other)


#: A one-pixel PNG, so an ImageField gets something it can actually accept.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _MediaFixture(TestCase):
    """Two real schools, each with a logo of its own.

    Two, deliberately. One school proves nothing about isolation: every check
    here is about what happens when the caller and the file belong to different
    customers, and that shape does not exist until there is a second school.
    """

    def setUp(self):
        from django.core.files.base import ContentFile as CF

        from schools.vs_schools.models import School, SchoolBranding, SchoolStatus
        from vs_tenants.context import clear_current_tenant, set_current_tenant
        from vs_user.models import User

        self.corona = School.objects.create(
            name="Corona Secondary School", slug="corona-secondary",
            status=SchoolStatus.ACTIVE,
        )
        self.greenfield = School.objects.create(
            name="Greenfield Academy", slug="greenfield-academy",
            status=SchoolStatus.ACTIVE,
        )

        self.bursar = User.objects.create_user(
            tenant=self.corona.tenant, email="bursar@corona.test",
            password="testpass123", status="ACTIVE",
            first_name="Ada", last_name="Okonkwo",
        )
        self.colleague = User.objects.create_user(
            tenant=self.corona.tenant, email="clerk@corona.test",
            password="testpass123", status="ACTIVE",
            first_name="Tunde", last_name="Bello",
        )
        # The same person, later, at a different school.
        self.at_greenfield = User.objects.create_user(
            tenant=self.greenfield.tenant, email="ada@greenfield.test",
            password="testpass123", status="ACTIVE",
            first_name="Ada", last_name="Okonkwo",
        )

        # Uploads happen inside a request, so the tenant is in context; that is
        # exactly how the storage learns whose file it is writing.
        set_current_tenant(self.corona.tenant)
        self.addCleanup(clear_current_tenant)
        self.branding = SchoolBranding.objects.create(school=self.corona)
        self.branding.logo.save("crest.png", CF(PNG_BYTES), save=True)
        self.name = self.branding.logo.name
        clear_current_tenant()

    def _client(self, user):
        from core.test_utils import TenantAPIClient

        return TenantAPIClient(user=user)

    def _signed(self, user, name=None):
        from core import media

        name = name or self.name
        return f"/media/{name}?{media.TOKEN_PARAM}={media.sign(name, user)}"


class MediaBindingTests(_MediaFixture):
    """The row has to know whose file it is before anything can check."""

    def test_upload_records_tenant_owner_and_field(self):
        row = StoredFile.objects.get(name=self.name)
        self.assertEqual(row.tenant_id, self.corona.tenant_id)
        self.assertEqual(row.owner, self.branding)
        self.assertEqual(row.owner_field, "logo")
        self.assertIsNone(row.revoked_at)

    def test_replacing_the_logo_retires_the_previous_one(self):
        from django.core.files.base import ContentFile as CF

        from vs_tenants.context import clear_current_tenant, set_current_tenant

        first = self.name
        set_current_tenant(self.corona.tenant)
        self.branding.logo.save("crest-v2.png", CF(PNG_BYTES), save=True)
        clear_current_tenant()

        old = StoredFile.objects.get(name=first)
        self.assertIsNotNone(old.revoked_at)
        self.assertEqual(bytes(old.content), b"")
        self.assertEqual(old.size, 0)
        # The replacement is live.
        self.assertIsNone(StoredFile.objects.get(name=self.branding.logo.name).revoked_at)

    def test_deleting_the_record_retires_its_file(self):
        self.branding.delete()
        row = StoredFile.objects.get(name=self.name)
        self.assertIsNotNone(row.revoked_at)
        self.assertEqual(bytes(row.content), b"")


class MediaViewTests(_MediaFixture):
    def test_anonymous_denied(self):
        resp = APIClient().get(f"/media/{self.name}")
        self.assertEqual(resp.status_code, 401)

    def test_signed_url_serves_the_school_its_own_logo(self):
        resp = self._client(self.bursar).get(self._signed(self.bursar))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp.content, PNG_BYTES)

    def test_missing_file_404(self):
        resp = self._client(self.bursar).get("/media/none/missing.png")
        self.assertEqual(resp.status_code, 404)

    # -- gate: the signature ------------------------------------------------
    def test_bare_path_without_a_signature_is_refused(self):
        """The name on its own is no longer a credential."""
        resp = self._client(self.bursar).get(f"/media/{self.name}")
        self.assertEqual(resp.status_code, 404)

    def test_a_forwarded_link_does_not_work_for_the_person_it_reaches(self):
        """Ada pastes her logo link into a thread; Tunde opens it and gets nothing.

        Both work at Corona and both may see the logo - Tunde only has to ask for
        it himself. What must never work is a URL travelling between people.
        """
        resp = self._client(self.colleague).get(self._signed(self.bursar))
        self.assertEqual(resp.status_code, 404)

    def test_an_expired_signature_is_refused(self):
        """The link that has been sitting in a chat thread since March."""
        from core import media

        dead = media.sign(self.name, self.bursar, exp=1)  # 1970, and then some
        resp = self._client(self.bursar).get(
            f"/media/{self.name}?{media.TOKEN_PARAM}={dead}",
        )
        self.assertEqual(resp.status_code, 404)

    def test_a_tampered_signature_is_refused(self):
        resp = self._client(self.bursar).get(self._signed(self.bursar) + "x")
        self.assertEqual(resp.status_code, 404)

    # -- gate: the tenant ----------------------------------------------------
    def test_the_bursar_who_changed_schools_cannot_replay_her_old_links(self):
        """Ada leaves Corona for Greenfield. Her history still holds the URL.

        Her Corona account is deactivated and her Greenfield one is live, so
        "are you signed in?" says yes. The file is Corona's, and she is asking
        as Greenfield, so the answer is no.
        """
        resp = self._client(self.at_greenfield).get(self._signed(self.at_greenfield))
        self.assertEqual(resp.status_code, 404)

    def test_the_other_school_cannot_read_the_logo_even_with_a_fresh_signature(self):
        """Isolating the tenant gate: a signature minted for the caller herself.

        This is not a replayed link; it is the best case an attacker could
        construct. The tenant mismatch alone must still refuse it.
        """
        from core import media
        from rest_framework.test import APIRequestFactory

        row = StoredFile.objects.get(name=self.name)
        request = APIRequestFactory().get(
            f"/media/{self.name}", {media.TOKEN_PARAM: media.sign(self.name, self.at_greenfield)},
        )
        request.user = self.at_greenfield
        request.tenant = self.greenfield.tenant
        self.assertFalse(media.authorize(request, row))

    # -- gate: the record ----------------------------------------------------
    def test_a_row_bound_to_nothing_is_never_served(self):
        """The shape every file written before this change has.

        Refusing is the deliberate choice: an unbound row cannot answer whose it
        is, and "I do not know" must not resolve to "everyone's".
        """
        StoredFile.objects.filter(name=self.name).update(
            tenant=None, owner_content_type=None, owner_object_id="",
        )
        resp = self._client(self.bursar).get(self._signed(self.bursar))
        self.assertEqual(resp.status_code, 404)

    def test_a_model_with_no_registered_policy_is_not_served(self):
        """Adding a FileField must not publish it by accident.

        Ticket attachments are the live example: they stream through the ticket
        endpoint, which checks internal-note visibility, and they register no
        media policy - so the generic route refuses them rather than quietly
        offering a second way in that checks less.
        """
        from django.contrib.contenttypes.models import ContentType

        from core import media

        row = StoredFile.objects.get(name=self.name)
        row.owner_content_type = ContentType.objects.get_for_model(StoredFile)
        row.owner_object_id = str(row.pk)
        row.save(update_fields=["owner_content_type", "owner_object_id"])
        self.assertIsNone(media.policy_for(StoredFile))
        resp = self._client(self.bursar).get(self._signed(self.bursar))
        self.assertEqual(resp.status_code, 404)

    # -- gate: revocation ----------------------------------------------------
    def test_a_revoked_file_is_refused_even_with_a_valid_signature(self):
        url = self._signed(self.bursar)
        from core.media import revoke

        revoke(self.name)
        resp = self._client(self.bursar).get(url)
        self.assertEqual(resp.status_code, 404)


class SignedUrlTests(_MediaFixture):
    def test_signed_url_binds_to_the_user_in_context(self):
        from urllib.parse import parse_qs, urlparse

        from core import media
        from core.media import signed_url
        from vs_tenants.context import (
            clear_current_tenant, set_current_audit_identity, set_current_tenant,
        )

        set_current_tenant(self.corona.tenant)
        set_current_audit_identity(actor_user=self.bursar, effective_user=self.bursar)
        self.addCleanup(clear_current_tenant)

        url = signed_url(self.name)
        parts = urlparse(url)
        self.assertEqual(parts.path, f"/media/{self.name}")
        query = parse_qs(parts.query)

        # The tenant assertion rides along, because the consumer is an <img src>
        # the frontend never gets to rewrite.
        self.assertEqual(query["tenant"], ["corona-secondary"])

        token = query[media.TOKEN_PARAM][0]
        self.assertTrue(media.signature_ok(token, self.name, self.bursar))
        self.assertFalse(media.signature_ok(token, self.name, self.colleague))
        # Bound to the file too, not just the person.
        self.assertFalse(media.signature_ok(token, "school_logos/other.png", self.bursar))

    def test_the_same_file_gets_the_same_url_inside_one_window(self):
        """A URL that changed every response would defeat the browser cache.

        Images are served ``Cache-Control: private, max-age=86400`` precisely so
        a crest is fetched once a day rather than once a screen, and the browser
        caches by full URL. A per-response signature would silently undo that,
        and would also make two payloads describing the same file disagree about
        where it lives - which is what ``/auth/me`` and the login response do.
        """
        from core.media import signed_url
        from vs_tenants.context import (
            clear_current_tenant, set_current_audit_identity, set_current_tenant,
        )

        set_current_tenant(self.corona.tenant)
        set_current_audit_identity(actor_user=self.bursar, effective_user=self.bursar)
        self.addCleanup(clear_current_tenant)

        self.assertEqual(signed_url(self.name), signed_url(self.name))
        # Still different per person, which is the property that matters.
        self.assertNotEqual(
            signed_url(self.name, user=self.bursar),
            signed_url(self.name, user=self.colleague),
        )

    def test_the_expiry_window_is_never_shorter_than_the_ttl(self):
        """Rounding to the next boundary alone would hand out near-dead URLs."""
        import datetime

        from core import media

        with self.settings(MEDIA_SIGNED_URL_TTL_SECONDS=900):
            # Walk a whole window second by second rather than trusting one
            # sample: the failure this guards against lives at the boundary, and
            # a single lucky timestamp would sail past it.
            for offset in range(0, 900):
                now = datetime.datetime.fromtimestamp(
                    900 * 1000 + offset, tz=datetime.timezone.utc,
                )
                remaining = media.expiry_bucket(now=now) - int(now.timestamp())
                self.assertGreaterEqual(remaining, 900, f"offset {offset}")
                self.assertLessEqual(remaining, 1800, f"offset {offset}")

    def test_no_identity_yields_no_url_rather_than_an_open_one(self):
        """Better a missing image than a link that works for anybody."""
        from core.media import signed_url
        from vs_tenants.context import clear_current_tenant

        clear_current_tenant()
        self.assertEqual(signed_url(self.name), "")
        self.assertEqual(signed_url(""), "")


class BackfillTests(_MediaFixture):
    """The migration that rescues media uploaded before any of this existed.

    Without it, turning binding on is not a tightening but an outage: every logo,
    receipt and vendor attachment already on the server is unbound, and unbound
    is refused. These tests run the migration's own function against the live
    registry rather than through the migration harness, so they stay in the fast
    suite while still exercising the code that ships.
    """

    #: Importable only by string - the module name starts with a digit.
    MIGRATION = "core.migrations.0006_backfill_storedfile_bindings"

    def _module(self):
        import importlib

        return importlib.import_module(self.MIGRATION)

    def _unbind(self, name=None):
        """Put a row back into the shape every pre-migration row is in."""
        StoredFile.objects.filter(name=name or self.name).update(
            tenant=None, owner_content_type=None, owner_object_id="", owner_field="",
        )

    def _run_backfill(self):
        from django.apps import apps as live_apps

        self._module().backfill(live_apps, None)

    def test_backfill_rebinds_a_logo_uploaded_before_the_change(self):
        self._unbind()
        self._run_backfill()

        row = StoredFile.objects.get(name=self.name)
        self.assertEqual(row.tenant_id, self.corona.tenant_id)
        self.assertEqual(row.owner, self.branding)
        self.assertEqual(row.owner_field, "logo")

    def test_backfilled_file_is_then_servable(self):
        """The point of the exercise: the school's logo loads again."""
        self._unbind()
        # Refused while unbound...
        self.assertEqual(
            self._client(self.bursar).get(self._signed(self.bursar)).status_code, 404,
        )
        self._run_backfill()
        # ...and served once the migration has said whose it is.
        self.assertEqual(
            self._client(self.bursar).get(self._signed(self.bursar)).status_code, 200,
        )

    def test_an_orphan_stays_unreadable(self):
        """A file no record points at is what the old model could never clean up."""
        from django.core.files.base import ContentFile as CF

        from core.storage import DatabaseStorage

        orphan = DatabaseStorage().save("school_logos/orphan.png", CF(PNG_BYTES))
        self._unbind(orphan)
        self._run_backfill()

        row = StoredFile.objects.get(name=orphan)
        self.assertIsNone(row.owner_content_type_id)
        self.assertEqual(
            self._client(self.bursar).get(self._signed(self.bursar, orphan)).status_code,
            404,
        )

    def test_every_binding_resolves_at_the_migrations_own_historical_state(self):
        """The dependencies must reach far enough forward to see every model.

        This is the silent failure the migration is most exposed to. Historical
        models come from the migration graph, not the live registry, so a
        dependency pinned one release too early hands the backfill a project
        state in which - say - PurchaseOrderVendorDelivery does not exist yet.
        ``get_model`` raises LookupError, the loop skips it by design, and every
        vendor PDF on the server stays unreadable with nothing anywhere saying
        so. Checking the live registry instead would pass and prove nothing.
        """
        from django.db.migrations.loader import MigrationLoader

        module = self._module()
        loader = MigrationLoader(None, ignore_no_migrations=True)
        node = ("core", "0006_backfill_storedfile_bindings")
        self.assertIn(node, loader.graph.nodes)
        historical = loader.project_state(nodes=[node]).apps

        problems = []
        # LATER_BINDINGS is deliberately not checked here: by definition those
        # models do not exist at this migration's state, which is why they are
        # in a second list and run from their own app's migration.
        for app_label, model_name, field_name, tenant_lookup in module.BINDINGS:
            try:
                model = historical.get_model(app_label, model_name)
            except LookupError:
                problems.append(f"{app_label}.{model_name}: model does not exist yet")
                continue
            if field_name not in {f.name for f in model._meta.get_fields()}:
                problems.append(f"{app_label}.{model_name}.{field_name}: no such field")
                continue
            try:
                # Forces the ORM to resolve the join without touching the database.
                list(model.objects.none().values_list("pk", field_name, tenant_lookup))
            except Exception as exc:
                problems.append(
                    f"{app_label}.{model_name}: {tenant_lookup} -> "
                    f"{type(exc).__name__}: {exc}"
                )

        self.assertEqual(problems, [], "\n".join(problems))

    def test_every_stored_file_field_is_either_backfilled_or_named_as_exempt(self):
        """A FileField added later must not fall out of the backfill unnoticed.

        The failure this guards against is silent in both directions: the new
        field's old rows never get bound, and nothing anywhere says so.
        """
        from django.apps import apps as live_apps
        from django.db import models as dj_models

        module = self._module()
        covered = {
            (app_label.lower(), model.lower(), field)
            for app_label, model, field, _ in (
                list(module.BINDINGS) + list(module.LATER_BINDINGS)
            )
        }
        # Nothing is exempt. Export and audit artefacts are absent because they
        # keep their storage key in a plain CharField rather than a FileField:
        # written straight to storage by a background job and served by their own
        # permission-checking views, so they are outside the FileField walk.
        missing = []
        for model in live_apps.get_models():
            for field in model._meta.get_fields():
                if not isinstance(field, dj_models.FileField):
                    continue
                key = (
                    model._meta.app_label.lower(),
                    model._meta.model_name.lower(),
                    field.name,
                )
                if key not in covered:
                    missing.append(key)

        self.assertEqual(
            sorted(missing), [],
            "These file fields have no entry in the backfill, so media uploaded "
            "to them before the binding existed will never become readable, and "
            "nothing anywhere would say so: " + repr(sorted(missing)),
        )


class RetiredOwnerTests(_MediaFixture):
    """What happens to a file when its record is archived rather than deleted.

    Deleting a record destroys its evidence, and should. Archiving is the
    opposite intent - the record is kept precisely so somebody can read it later
    - so archiving must not destroy anything. What it does do is stop the file
    answering a URL that is already loose in the world, because a record taken
    out of service should not go on serving its evidence to whoever kept a link.

    ``AcademicSession`` is used as the owner here because it is a real model
    carrying a real ``archived_at`` column, so these exercise the field
    detection rather than a stand-in for it.
    """

    def setUp(self):
        super().setUp()
        import datetime

        from schools.vs_academics.models import AcademicSession

        from core import media

        self.year = AcademicSession.all_objects.create(
            tenant=self.corona.tenant, name="2025/2026",
            start_date=datetime.date(2025, 9, 1),
            end_date=datetime.date(2026, 7, 31),
        )
        # Bind the fixture's file to that year, and give it a policy for the
        # duration of the test only - the registry is process-wide.
        from django.contrib.contenttypes.models import ContentType

        StoredFile.objects.filter(name=self.name).update(
            owner_content_type=ContentType.objects.get_for_model(AcademicSession),
            owner_object_id=str(self.year.pk),
            owner_field="prospectus",
        )
        self._saved_policies = dict(media._POLICIES)
        self.addCleanup(lambda: media._POLICIES.clear() or
                        media._POLICIES.update(self._saved_policies))

    def _register(self, **kwargs):
        from schools.vs_academics.models import AcademicSession

        from core import media

        media.register_policy(AcademicSession, lambda request, owner: True, **kwargs)

    def _archive(self):
        from django.utils import timezone as dj_tz

        type(self.year).all_objects.filter(pk=self.year.pk).update(
            archived_at=dj_tz.now(),
        )

    def test_is_retired_reads_the_conventions_actually_in_use(self):
        """There is no shared archivable base, so this reads what modules use."""
        from core.media import is_retired

        class Live: pass
        class Stamped: archived_at = "2026-08-27"
        class Flagged: is_archived = True
        class Cleared: archived_at = None

        self.assertFalse(is_retired(Live()))
        self.assertFalse(is_retired(Cleared()))
        self.assertTrue(is_retired(Stamped()))
        self.assertTrue(is_retired(Flagged()))

    def test_a_live_record_serves_its_file(self):
        self._register()
        resp = self._client(self.bursar).get(self._signed(self.bursar))
        self.assertEqual(resp.status_code, 200)

    def test_archiving_the_record_closes_the_url(self):
        self._register()
        self._archive()
        resp = self._client(self.bursar).get(self._signed(self.bursar))
        self.assertEqual(resp.status_code, 404)

    def test_archiving_does_not_destroy_the_bytes(self):
        """The difference between archiving and deleting, in one assertion.

        Corona archives the 2025/2026 year. An auditor asking what happened that
        year still needs what was filed against it, and the archive exists to
        keep exactly that. Closing the URL is a door; emptying the row would be
        a bonfire.
        """
        self._register()
        self._archive()
        row = StoredFile.objects.get(name=self.name)
        self.assertEqual(bytes(row.content), PNG_BYTES)
        self.assertIsNone(row.revoked_at)

    def test_a_module_can_opt_into_serving_its_archived_records(self):
        """Refusing is the default, not the only answer - but it is said out loud."""
        self._register(serve_when_retired=True)
        self._archive()
        resp = self._client(self.bursar).get(self._signed(self.bursar))
        self.assertEqual(resp.status_code, 200)
