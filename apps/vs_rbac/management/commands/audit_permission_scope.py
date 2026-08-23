"""Find permissions a tenant may hold that write rows every tenant shares.

Run it after adding permissions, or after adding a view::

    python manage.py audit_permission_scope
    python manage.py audit_permission_scope --strict   # exit 1 on a finding

Why this exists. The school roles screen was the first surface to show a school
administrator the permission registry, and putting 343 keys in front of a school
turned up five that wrote a GLOBAL table - one row set shared by every school on
the platform. The worst was
``communication.notification_templates.configure``: its ViewSet scoped nothing
and had no platform guard, so a school could rewrite the message templates every
other school receives. It was found by hand. This is the same three questions,
asked mechanically, so the next one is found on the next run instead:

    1. Does the key let you WRITE?
    2. Is the table behind it global - no route to a tenant at all?
    3. Is there a platform guard in front of it?

**Read the limits before trusting a clean run.** This walks resolved routes and
reads class source, so it cannot see everything:

* A view whose ``rbac_permission`` is a property is scraped for key literals
  rather than evaluated, so it may over-report which keys a view demands.
* Keys enforced in a serializer, a service or a ViewSet action reached by a
  router this walker misses are reported under "not reached", NOT as safe.
* The model behind a view is resolved from ``queryset``, the serializer's Meta,
  ``Model.objects`` in the body, then the key's own resource word. Anything left
  unresolved is reported, never assumed.

A clean run means "nothing found by these means", which is weaker than "nothing
there". The unreached list is the part a human still has to read.
"""
from __future__ import annotations

import inspect
import re

from django.core.management.base import BaseCommand
from django.urls import URLPattern, URLResolver, get_resolver

#: Verbs that change something. A read on a shared table is fine - a school has
#: to see the currency list; it just may not edit it for everybody.
WRITE_VERBS = {
    "create", "update", "delete", "manage", "configure", "edit", "import",
    "generate", "post", "approve", "approve_high_value", "approve_senior",
    "reject", "reverse", "writeoff", "cancel", "close", "lock", "reopen",
    "settle", "pay", "submit", "send", "email", "email_statement", "allocate",
    "acquire", "activate", "depreciate", "dispose", "establish", "file",
    "reconcile", "replenish", "share", "resolve", "run", "rollback", "assign",
    "revoke", "override", "override_variance", "suspend", "reactivate",
    "impersonate", "attach", "start", "end",
}

#: A column that ties a row to one school.
TENANT_COLUMNS = {"tenant", "entity", "school", "branch"}

#: Keys that match the shape but have been reviewed and are deliberate.
#:
#: Every entry needs a reason and something that proves it. An allowlist without
#: a test behind it is just a way to stop the tool complaining.
REVIEWED = {
    "onboarding.progress.reactivate": (
        "Writes Tenant, which is global by definition. Left tenant-holdable on "
        "purpose: the view demands the PLATFORM tenant as a second gate, and "
        "vs_onboarding.tests_lifecycle."
        "test_a_school_caller_holding_the_key_is_still_refused grants it to a "
        "school and asserts 403. Reclassifying would delete that check."
    ),
}

GUARD_PATTERN = re.compile(
    r"_is_platform|platform_methods|platform_decision|PlatformDecisionAllowed"
    r"|IsPlatformUser|kind\s*==\s*Tenant\.Kind\.PLATFORM|is_platform"
)
KEY_PATTERN = re.compile(r"""["']([a-z_]+\.[a-z_]+\.[a-z_]+)["']""")
MODEL_PATTERN = re.compile(r"\b([A-Z]\w+)\.(?:objects|all_objects)\b")


class Command(BaseCommand):
    help = "Report tenant-holdable permissions that write a globally shared table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict", action="store_true",
            help="Exit non-zero when a global-table write is found (for CI).",
        )
        parser.add_argument(
            "--show-unreached", action="store_true",
            help="Also list keys no resolved route demands.",
        )

    def handle(self, *args, **options):
        from vs_rbac.models import Permission, PermissionScope

        self._models = {}
        tenant_keys = set(
            Permission.objects
            .filter(is_active=True, scope=PermissionScope.TENANT)
            .values_list("key", flat=True)
        )

        seen = {}
        for cls, source in self._views():
            model = self._model_for(cls, source)
            guarded = bool(GUARD_PATTERN.search(source)) if source else False
            for key in self._keys_for(cls, source) & tenant_keys:
                row = seen.setdefault(key, {"models": set(), "guarded": False})
                row["guarded"] = row["guarded"] or guarded
                resolved = model or self._model_by_resource(key)
                if resolved is not None:
                    row["models"].add(resolved)

        findings, unresolved, reviewed = [], [], []
        for key, row in sorted(seen.items()):
            if key.rsplit(".", 1)[-1] not in WRITE_VERBS:
                continue
            if not row["models"]:
                unresolved.append(key)
            elif not any(self._is_scoped(m) for m in row["models"]):
                if key in REVIEWED:
                    reviewed.append(key)
                else:
                    findings.append((key, row))

        self._report(
            findings, unresolved, reviewed,
            sorted(tenant_keys - set(seen)), options,
        )

        if options["strict"] and findings:
            raise SystemExit(1)

    # ── the walk ─────────────────────────────────────────────────────────────

    def _views(self):
        def walk(patterns):
            for p in patterns:
                if isinstance(p, URLResolver):
                    yield from walk(p.url_patterns)
                elif isinstance(p, URLPattern):
                    yield p.callback

        for cb in walk(get_resolver().url_patterns):
            cls = getattr(cb, "view_class", None) or getattr(cb, "cls", None)
            if cls is None:
                continue
            try:
                yield cls, inspect.getsource(cls)
            except (OSError, TypeError):
                yield cls, ""

    def _keys_for(self, cls, source):
        raw = getattr(cls, "rbac_permission", None)
        if isinstance(raw, str):
            return {raw}
        if isinstance(raw, (list, tuple, set)):
            return {k for k in raw if isinstance(k, str)}
        # A property switching on the HTTP method cannot be read off the class,
        # so fall back to the literals in its body.
        return set(KEY_PATTERN.findall(source)) if raw is not None and source else set()

    # ── resolving the table ──────────────────────────────────────────────────

    def _index(self):
        if not self._models:
            from django.apps import apps as registry

            for m in registry.get_models():
                self._models.setdefault(m.__name__, m)
        return self._models

    def _model_for(self, cls, source):
        qs = getattr(cls, "queryset", None)
        if qs is not None and hasattr(qs, "model"):
            return qs.model
        meta = getattr(getattr(cls, "serializer_class", None), "Meta", None)
        if getattr(meta, "model", None) is not None:
            return meta.model
        for name in MODEL_PATTERN.findall(source or ""):
            found = self._index().get(name)
            if found is not None:
                return found
        return None

    def _model_by_resource(self, key):
        """Last resort: the key's middle word names the thing it acts on.

        Action endpoints (``.post``, ``.submit``) fetch one row through a helper,
        so no ``Model.objects`` appears in the class body at all.
        """
        wanted = key.split(".")[1].replace("_", "")
        module = key.split(".")[0]
        # Try the module-prefixed name first: the onboarding step model is
        # OnboardingTask, not Task, and "task" alone finds a CodeX to-do.
        for candidate in (module + wanted, wanted):
            matches = [
                m for n, m in self._index().items() if n.lower() == candidate
            ]
            for m in matches:
                if module in m._meta.app_label.replace("vs_", ""):
                    return m
            if len(matches) == 1 and matches[0]._meta.app_label.replace(
                "vs_", ""
            ).startswith(module[:4]):
                return matches[0]
        # Names collide across apps. A guess that lands on a stranger's table
        # reports the wrong thing, so leave it unresolved and let a human look.
        return None

    def _is_scoped(self, model, _depth=0):
        """Can a row be traced back to one school, directly or through a parent?

        A JournalLine carries no tenant, but every line belongs to a Journal that
        does, so the table is not shared. Only a model with no route to a tenant
        at all is global, and that is the shape worth flagging.

        **A foreign key to a User is never that route.** Every user belongs to a
        tenant, so following ``created_by`` would make any table with an author
        column look scoped - and NotificationTemplate, the one live hole this
        whole exercise started from, has ``created_by``. Following it would have
        cleared the very row that could rewrite every school's mail. Authorship
        records who touched a row; it does not decide who the row belongs to.
        """
        fields = {f.name for f in model._meta.fields}
        if fields & TENANT_COLUMNS:
            return True
        if _depth >= 2:
            return False
        user_model = self._user_model()
        for f in model._meta.fields:
            if not f.is_relation or f.related_model in (None, model):
                continue
            if f.related_model is user_model:
                continue
            if self._is_scoped(f.related_model, _depth + 1):
                return True
        return False

    def _user_model(self):
        from django.contrib.auth import get_user_model

        return get_user_model()

    # ── output ───────────────────────────────────────────────────────────────

    def _report(self, findings, unresolved, reviewed, unreached, options):
        if findings:
            self.stdout.write(self.style.ERROR(
                f"\n  {len(findings)} tenant-holdable key(s) write a globally "
                f"shared table:\n"
            ))
            for key, row in findings:
                guard = "guarded" if row["guarded"] else "NO GUARD"
                tables = ", ".join(sorted(m.__name__ for m in row["models"]))
                self.stdout.write(f"    [{guard}]  {key:46} {tables}")
            self.stdout.write(
                "\n  Classify each in the seeder that registers it, and add a "
                "migration to move existing rows.\n"
            )
        else:
            self.stdout.write(self.style.SUCCESS(
                "\n  No tenant-holdable key writes a globally shared table.\n"
            ))

        for key in reviewed:
            self.stdout.write(
                f"  Matches the shape, reviewed and deliberate: {key}\n"
                f"    {REVIEWED[key]}\n"
            )

        if unresolved:
            self.stdout.write(self.style.WARNING(
                f"  {len(unresolved)} write key(s) whose table could not be "
                f"resolved - check these by hand:"
            ))
            for key in unresolved:
                self.stdout.write(f"    {key}")
            self.stdout.write("")

        self.stdout.write(
            f"  {len(unreached)} key(s) reached by no resolved route. These are "
            f"NOT cleared by this run:\n"
            f"  some are enforced in a serializer or a ViewSet action this "
            f"walker cannot see,\n"
            f"  and some are seeded ahead of the feature that will use them."
        )
        if options["show_unreached"]:
            for key in unreached:
                self.stdout.write(f"    {key}")
        else:
            self.stdout.write("  Pass --show-unreached to list them.\n")
