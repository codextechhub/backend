"""The two update endpoints, and the rows they used to be unable to touch.

Two defects, one shape: a rule that existed in the model and nowhere a caller
could reach.

* A school's slug is editable until it goes live and frozen after, enforced in
  ``School.save()`` since 474a01c - but ``SchoolUpdateSerializer`` exposed no
  ``slug``, so the typo correction that rule exists to permit could only be
  made from a shell.
* ``Branch._type`` called itself "optional freeform" and was declared
  ``CharField(max_length=80)`` with no ``blank``. Every row created outside the
  serializers stored ``""``, and ``BranchUpdateSerializer.update()`` runs
  ``full_clean()`` over the whole instance, so those rows could never be
  updated through the API again - refused over a field the caller had not
  touched, and not even told which one.
"""
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import make_branch, make_school, make_vision_user
from vs_tenants.models import Branch, BranchStatus, Tenant

from .models import School, SchoolStatus


class SchoolSlugUpdateTests(TestCase):
    """``PATCH /v1/schools/<slug>/update/`` with a ``slug``."""

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="slug-update@example.com", super_admin=True
        )

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.vision_user)
        return client

    def _url(self, school):
        return reverse("school-update", kwargs={"slug": school.slug})

    def _pending_school(self, *, slug, name="Bright Star"):
        """A school that has not gone live, with the main branch every school has."""
        school = make_school(slug=slug, name=name, status=SchoolStatus.PENDING)
        make_branch(school, name="Main Campus", status=BranchStatus.PENDING)
        return school

    # --- the correction the rule exists to permit ---------------------------

    def test_a_pending_school_can_correct_its_slug(self):
        school = self._pending_school(slug="bright-star", name="Bright Star Academy")

        response = self._client().patch(
            self._url(school), {"slug": "bright-star-academy"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        school.refresh_from_db()
        self.assertEqual(school.slug, "bright-star-academy")

    def test_the_corrected_slug_reaches_the_tenant(self):
        """The only address that matters.

        The school's own slug is the ``/v1/i/<slug>/`` path key; the *tenant's*
        is the sign-in host. A correction that stopped at the school row would
        leave Bright Star's admins still signing in at the misspelt host, which
        is the whole point of the mirror in ``School.save()``.
        """
        school = self._pending_school(slug="corona-secondry", name="Corona Secondary")

        response = self._client().patch(
            self._url(school), {"slug": "corona-secondary"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            Tenant.objects.get(pk=school.tenant_id).slug, "corona-secondary",
        )

    def test_the_response_carries_the_new_slug(self):
        """``lookup_field`` is the slug, so the view re-read the row by the URL
        key after the write. On a rename that key no longer resolves, and the
        request that had just succeeded came back a 404."""
        school = self._pending_school(slug="lookup-old", name="Lookup School")

        response = self._client().patch(
            self._url(school), {"slug": "lookup-new"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["slug"], "lookup-new")

    def test_a_slug_is_normalised_rather_than_rejected_on_its_casing(self):
        """The create path normalises "Bright Star" into a slug; correcting a
        typo behaves the same way instead of answering with a regex."""
        school = self._pending_school(slug="casing-old", name="Casing School")

        response = self._client().patch(
            self._url(school), {"slug": "  Casing New  "}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        school.refresh_from_db()
        self.assertEqual(school.slug, "casing-new")

    def test_the_other_fields_still_update_alongside(self):
        school = self._pending_school(slug="mixed-edit", name="Mixed Edit")

        response = self._client().patch(
            self._url(school),
            {"slug": "mixed-edit-fixed", "motto": "Learning first"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        school.refresh_from_db()
        self.assertEqual(school.slug, "mixed-edit-fixed")
        self.assertEqual(school.motto, "Learning first")

    # --- the refusals -------------------------------------------------------

    def test_a_reserved_slug_is_refused_as_a_field_error(self):
        """`support` is a platform hostname. Left to the model this surfaces
        from ``full_clean()``; the caller should get an ordinary field error."""
        school = self._pending_school(slug="reserved-attempt", name="Support Academy")

        response = self._client().patch(
            self._url(school), {"slug": "support"}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("slug", response.data["error"]["detail"])
        school.refresh_from_db()
        self.assertEqual(school.slug, "reserved-attempt")

    def test_a_slug_another_school_already_holds_is_refused(self):
        self._pending_school(slug="taken-slug", name="Incumbent School")
        school = self._pending_school(slug="hopeful-slug", name="Hopeful School")

        response = self._client().patch(
            self._url(school), {"slug": "taken-slug"}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("slug", response.data["error"]["detail"])
        school.refresh_from_db()
        self.assertEqual(school.slug, "hopeful-slug")

    def test_a_duplicate_slug_is_refused_before_the_database_sees_it(self):
        """A field error, not an IntegrityError rendered as "A record with
        these details already exists." - the caller has to know it was the
        slug, and be offered somewhere to go."""
        self._pending_school(slug="clash-target", name="Clash Target")
        school = self._pending_school(slug="clash-source", name="Clash Source")

        response = self._client().patch(
            self._url(school), {"slug": "clash-target"}, format="json",
        )

        self.assertEqual(response.data["error"]["code"], "REQUEST_ERROR")
        self.assertIn("suggestions", str(response.data["error"]["detail"]))

    def test_a_slug_held_by_a_non_school_tenant_is_refused(self):
        """``School.save()`` mirrors onto the tenant with a queryset
        ``update()``, which cannot raise a field error - only an IntegrityError
        against ``Tenant.slug``. A VIGIL clinic group holding the name is
        enough, and there is no school row to have caught it."""
        Tenant.objects.create(
            name="Riverside Clinics",
            slug="riverside",
            kind=Tenant.Kind.ORGANIZATION,
        )
        school = self._pending_school(slug="riverside-school", name="Riverside School")

        response = self._client().patch(
            self._url(school), {"slug": "riverside"}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        school.refresh_from_db()
        self.assertEqual(school.slug, "riverside-school")

    def test_an_empty_slug_is_refused(self):
        school = self._pending_school(slug="empty-attempt", name="Empty Attempt")

        response = self._client().patch(
            self._url(school), {"slug": "!!!"}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        school.refresh_from_db()
        self.assertEqual(school.slug, "empty-attempt")

    # --- the freeze ---------------------------------------------------------

    def test_a_live_school_cannot_move_its_address(self):
        school = make_school(slug="live-academy", name="Live Academy")
        make_branch(school, name="Main Campus")
        self.assertEqual(school.status, SchoolStatus.ACTIVE)

        response = self._client().patch(
            self._url(school), {"slug": "live-academy-renamed"}, format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_SLUG_FROZEN")
        self.assertIn("is live", response.data["message"])
        self.assertIn("cannot move", response.data["message"])
        school.refresh_from_db()
        self.assertEqual(school.slug, "live-academy")
        self.assertEqual(Tenant.objects.get(pk=school.tenant_id).slug, "live-academy")

    def test_a_school_suspended_after_go_live_is_still_frozen(self):
        """Not ``status == ACTIVE``: a school off over an unpaid invoice would
        otherwise rename itself, pay, come back, and find every parent's
        sign-in address dead."""
        school = make_school(
            slug="suspended-live", name="Suspended School",
            activated_at=timezone.now(),
        )
        make_branch(school, name="Main Campus")
        School.objects.filter(pk=school.pk).update(status=SchoolStatus.SUSPENDED)

        response = self._client().patch(
            self._url(school), {"slug": "suspended-renamed"}, format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_SLUG_FROZEN")

    def test_a_live_school_may_still_edit_everything_else(self):
        """The freeze is on the address, not on the row."""
        school = make_school(slug="live-editable", name="Live Editable")
        make_branch(school, name="Main Campus")

        response = self._client().patch(
            self._url(school), {"motto": "Still editable"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        school.refresh_from_db()
        self.assertEqual(school.motto, "Still editable")

    def test_a_live_school_resending_its_own_slug_is_not_refused(self):
        """A UI that PATCHes the whole form back is not asking for a rename."""
        school = make_school(slug="live-noop", name="Live No-op")
        make_branch(school, name="Main Campus")

        response = self._client().patch(
            self._url(school),
            {"slug": "live-noop", "motto": "Unchanged address"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        school.refresh_from_db()
        self.assertEqual(school.slug, "live-noop")

    # --- the permission gate ------------------------------------------------

    def test_a_caller_without_the_key_cannot_move_a_school(self):
        """Vision staff, holding no role that grants platform.schools.update."""
        school = self._pending_school(slug="gated-school", name="Gated School")
        plain_staff = make_vision_user(email="slug-nokey@example.com")

        response = self._client(plain_staff).patch(
            self._url(school), {"slug": "gated-moved"}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)
        school.refresh_from_db()
        self.assertEqual(school.slug, "gated-school")

    def test_an_anonymous_caller_cannot_move_a_school(self):
        school = self._pending_school(slug="anon-school", name="Anon School")

        response = APIClient().patch(
            self._url(school), {"slug": "anon-moved"}, format="json",
        )

        self.assertIn(response.status_code, (401, 403))
        school.refresh_from_db()
        self.assertEqual(school.slug, "anon-school")

    # --- what is deliberately not exposed -----------------------------------

    def test_the_display_name_is_not_writable_here(self):
        """``name`` is left off this serializer on purpose: the importer
        resolves a school by name when a row carries no slug, so a rename turns
        a school's own import file into a request to create a second school.
        That needs its own decision."""
        school = self._pending_school(slug="name-locked", name="Name Locked")

        response = self._client().patch(
            self._url(school), {"name": "Renamed School"}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        school.refresh_from_db()
        self.assertEqual(school.name, "Name Locked")


class BranchUpdateBlankFieldTests(TestCase):
    """``PATCH /v1/schools/<slug>/branches/<code>/update/`` over a legacy row."""

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="branch-blank@example.com", super_admin=True
        )
        cls.school = make_school(slug="blank-type-school", name="Blank Type School")
        cls.main = make_branch(cls.school, name="Head Office")

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.vision_user)
        return client

    def _url(self, branch):
        return reverse(
            "branch-update",
            kwargs={"slug": self.school.slug, "code": branch.code},
        )

    def _row_with_blank_type(self, name="Legacy Campus"):
        """A branch made the way ``seed_import``, a data migration and the
        shell make one: straight through the manager, no serializer, so
        ``_type`` is never supplied."""
        return Branch.objects.create(
            tenant=self.school.tenant,
            name=name,
            is_main=False,
            status=BranchStatus.ACTIVE,
        )

    def test_a_row_created_outside_the_serializer_has_a_blank_type(self):
        """The premise. If this ever stops being true the tests below stop
        testing anything."""
        branch = self._row_with_blank_type()

        self.assertEqual(branch._type, "")

    def test_a_branch_with_a_blank_type_can_be_updated(self):
        branch = self._row_with_blank_type()

        response = self._client().patch(
            self._url(branch), {"address": "14 Admiralty Way, Lekki"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        branch.refresh_from_db()
        self.assertEqual(branch.address, "14 Admiralty Way, Lekki")

    def test_such_a_branch_can_also_be_given_a_type(self):
        branch = self._row_with_blank_type(name="Typed Campus")

        response = self._client().patch(
            self._url(branch), {"_type": "Secondary"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        branch.refresh_from_db()
        self.assertEqual(branch._type, "Secondary")

    def test_a_type_may_be_cleared_again(self):
        """``blank=True`` has to mean it on the way in as well as on the way
        out, or the field is merely optional once."""
        branch = self._row_with_blank_type(name="Clearable Campus")
        Branch.all_objects.filter(pk=branch.pk).update(_type="Nursery")

        response = self._client().patch(
            self._url(branch), {"_type": ""}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        branch.refresh_from_db()
        self.assertEqual(branch._type, "")

    # --- the field that is genuinely required -------------------------------

    def test_a_genuinely_required_field_still_refuses_the_write(self):
        """``name`` was deliberately not swept: a site must be named. The point
        of the sweep was to stop *optional* columns behaving like required
        ones, not to make every column optional."""
        branch = self._row_with_blank_type(name="Nameless Campus")
        Branch.all_objects.filter(pk=branch.pk).update(name="")

        response = self._client().patch(
            self._url(branch), {"address": "Somewhere"}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)

    def test_that_refusal_names_the_field(self):
        """The third defect. ``full_clean()`` raises ``{"name": [...]}`` and
        nothing on this path was translating it, so the caller was told a field
        could not be blank on an endpoint that writes eight of them - and never
        which one."""
        branch = self._row_with_blank_type(name="Unnamed Campus")
        Branch.all_objects.filter(pk=branch.pk).update(name="")

        response = self._client().patch(
            self._url(branch), {"address": "Somewhere"}, format="json",
        )

        detail = response.data["error"]["detail"]
        self.assertIsInstance(detail, dict)
        self.assertIn("name", detail)

    # --- the rest of the sweep ----------------------------------------------

    def test_a_lifecycle_row_with_no_from_state_validates(self):
        """``BranchCreateSerializer`` writes ``from_state=""`` for a creation
        event, which has no state to come from, and ``Branch.transition``
        writes ``actor_id=""`` for a system-driven move. Both columns were
        non-blank with no default - the same shape as ``_type``, and the
        leftovers of the sweep that stopped at ``reason``."""
        from vs_tenants.models import BranchLifecycle

        event = BranchLifecycle.objects.create(
            branch=self.main, from_state="", to_state=BranchStatus.PENDING,
            actor_id="", reason="",
        )

        event.full_clean()  # would raise on from_state, actor_id and reason
