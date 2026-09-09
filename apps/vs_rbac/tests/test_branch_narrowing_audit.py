"""Every lookup by id, on a model that carries a branch, inside a request handler.

The same defect has now been found three times, in three apps, by three
different routes: a list narrowed to the caller's branches sitting beside a
detail route that takes an id and asks nothing. ``vs_config`` could be written
across branches through ``?branch=``; an import batch's uploaded spreadsheet
could be downloaded from another site by changing the id in the address; every
route into a bank account except the list would open, rename, import onto and
reconcile another site's account.

Each was found by a person reading code. This finds the fourth.

What it checks
--------------

One rule, and it is deliberately narrow enough to be mechanical: **a lookup
keyed by an id that came from outside, on a model carrying a ``branch`` column,
inside a function that takes a ``request``, must narrow to the caller's
branches.** The three conditions together are what make it a reachability
decision rather than an internal read. A service that receives an already
resolved row is not asked, and neither is a lookup by a literal id.

The models are read from the app registry rather than listed here, so a
``branch`` column added tomorrow is covered the day it lands, on every route
that reads it, without anybody remembering this file exists.

What it does not check
----------------------

This reads source, not traffic. It cannot tell whether a narrowing is *correct*
- whether ``include_shared`` is the right way round, or whether the refusal is
the 404 an unknown id gets rather than a 403 that confirms the row exists. The
end-to-end tests beside each app answer that (``vs_finance``'s
``tests_branch_scope``, ``vs_import_data``'s ``tests_branch_scope``,
``vs_config``'s ``ConfigurationBranchEntitlementTests``) and this does not
replace them. It answers the one question they structurally cannot: is there a
route nobody has written a test for.

The two registries
------------------

A flagged lookup belongs in exactly one of them, and every entry carries its
reason:

``SETTLED_ELSEWHERE``
    The branch question is answered, just not by this queryset. Usually by
    ``inherited_branch_id`` on the row being created a few lines below, which
    refuses a caller continuing another site's chain; sometimes because the
    model is tenant-level in practice, or because the id is the caller's own.

``UNNARROWED``
    Nothing answers it. These are open holes, recorded so the audit can pass
    and the next one still fails it. They are debt, not decisions, and the note
    on each says what a caller could reach.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

from django.apps import apps as django_apps
from django.test import SimpleTestCase

import vs_rbac

#: The application source, from this package's own location.
APPS_ROOT = Path(vs_rbac.__file__).resolve().parent.parent

#: Directories with no request handlers in them.
SKIP_DIRS = {"__pycache__", "migrations", "node_modules", "static", "media"}

#: Managers a lookup can start from.
MANAGERS = {"objects", "all_objects", "_default_manager", "_base_manager"}

#: Queryset methods that resolve rows.
LOOKUPS = {"filter", "get", "exclude"}

#: Keyword arguments naming a row by its primary key.
ID_KWARGS = {"pk", "id", "pk__in", "id__in"}

#: Anything whose presence in the statement means the branch question was asked.
#:
#: ``vs_rbac.scoping`` is the platform's answer and the first four are its
#: public surface. The rest are the domain wrappers over it - a school app asks
#: the same question about a staff record or a child through a helper that also
#: knows what "visible" means for that model - and they count because they end
#: up calling the same function.
NARROWERS = (
    "branch_q",
    "branch_visible",
    "branch_scope",
    "caller_branch_ids",
    "visible_branch_ids",
    "_branch_visible",
    "_branch_scoped",
    "_bank_or_404",
    "scope_staff",
    "scope_students",
    "scope_classes",
    "scope_to_visible_branches",
    "scope_events_to_caller",
)

#: Flagged lookups whose branch question is answered somewhere else. Keyed by
#: ``<path>::<function>::<Model>``, which survives ordinary edits to the file.
SETTLED_ELSEWHERE = {
    "vs_procurement/views/orders.py::post::PurchaseRequisition":
        "The requisition fixes the order's branch and inherited_branch_id "
        "refuses a caller who may not work in it. The source document decides, "
        "so there is deliberately no branch input on this route.",
    "vs_procurement/views/receiving.py::post::PurchaseOrder":
        "A receipt against an order takes that order's branch through "
        "inherited_branch_id, which refuses a caller continuing another site's "
        "chain.",
    "vs_procurement/views/receiving.py::patch::PurchaseOrder":
        "Re-pointing a draft vendor invoice at an order. The invoice was "
        "already resolved through the entity's scoped resolver, and the order "
        "must match its vendor.",
    "vs_finance/views_ops/pettycash.py::post::PettyCashFund":
        "A voucher continues the fund's chain, and inherited_branch_id on the "
        "voucher is what stops a Lekki custodian spending Ikeja's float by "
        "naming its id. Said in a comment at the call site.",
    "vs_admin_console/views.py::start::User":
        "Choosing somebody to impersonate. Pinned to the asserted tenant, and "
        "impersonation is a platform act that no branch narrows: an operator "
        "standing in for a branch administrator is the point of it.",
    "vs_audit/views.py::list::User":
        "Turning actor ids that are already in the audit rows the caller can "
        "read into display names. It decides nothing about reachability.",
    "vs_exports/views.py::post::User":
        "Sharing a saved export with colleagues in the same tenant. Sharing "
        "across sites is the feature, not a leak.",
    "vs_rbac/views.py::post::TenantRoleTemplate":
        "The role being requested. Role templates belong to the tenant, and a "
        "branch-pinned grant of one is an assignment, not a template.",
    "vs_user/views/auth.py::post::User":
        "The caller's own row, taken from the token they presented and checked "
        "against request.user. There is no other person in the question.",
    "vs_workflow/views.py::compare::WorkflowTemplate":
        "The other version of the template already open, pinned to its code, "
        "document type and tenant, so only its own history is reachable.",
    "schools/vs_calendar/views/teachers.py::get::User":
        "Whose timetable to show. Deliberately school-wide, and narrowing it "
        "would break the case the screen exists for: a teacher works at two "
        "sites, so ``teaching_users`` is unnarrowed on purpose and says why, "
        "and a grid narrower than the picker would make them unschedulable at "
        "the second site.",
    "schools/vs_staff/views/directory.py::_is_own_record::StaffProfile":
        "Whether this record is the caller's own, filtered on their own user "
        "id. It decides which permission key applies, and there is nobody else "
        "in the question for a branch to narrow.",
    "schools/vs_staff/views/leave.py::_is_own::StaffProfile":
        "The same self-check, deciding between applying for leave and "
        "approving somebody else's.",
    "schools/vs_staff/views/records.py::_is_own::StaffProfile":
        "The same self-check again, on the records screens.",
    "vs_tickets/views.py::assign::User":
        "The person a ticket is being handed to. A support desk assigns across "
        "sites by design, and assign_ticket validates the assignee.",
}

#: Flagged lookups nothing answers. Open holes, recorded so the next one fails.
#:
#: Both are procurement master data, and both are waiting on the same decision
#: rather than on somebody finding the time. Procurement's own helpers -
#: ``_document_or_404`` and ``_branch_visible`` - read an absent branch as a
#: scope of its own that a branch-pinned caller is not in, which is right for a
#: purchase and wrong for a catalogue: applied here they would hide the school's
#: central store and its school-wide vendors from every site. Narrowing these
#: means first saying whether procurement's master data takes the catalogue
#: reading that vs_academics and vs_calendar take.
UNNARROWED = {
    "vs_procurement/views/stock.py::_location::StockLocation":
        "The resolver behind stock location read and manage, so another site's "
        "store is readable and editable by id. Its list is not narrowed either, "
        "so this is the whole model rather than a detail route that drifted.",
    "vs_procurement/views/vendors.py::get::Vendor":
        "Per-vendor spend and performance for a vendor named by id.",
}


def _rel(path: Path) -> str:
    return str(path.relative_to(APPS_ROOT))


def branch_carrying_models() -> set[str]:
    """Every installed model with a ``branch`` field, by class name.

    Names rather than model classes because the audit reads source, where a
    lookup is spelled ``BankAccount.objects``. Two apps sharing a class name
    would widen the audit rather than narrow it, which is the safe direction.
    """
    return {
        model.__name__
        for model in django_apps.get_models()
        if any(getattr(field, "name", "") == "branch" for field in model._meta.get_fields())
    }


def _request_handlers(tree: ast.AST):
    """Functions serving a request, which is what makes a lookup a decision.

    Both spellings count. A plain view method takes ``request`` as an argument;
    a mixin helper reaches it as ``self.request`` and takes only the id, and
    the school apps are written that way throughout. Reading the argument list
    alone would skip every one of them, which is most of a resolver layer.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        argument_names = {a.arg for a in node.args.args + node.args.kwonlyargs}
        if "request" in argument_names:
            yield node
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Attribute) and inner.attr == "request"
                    and isinstance(inner.value, ast.Name) and inner.value.id == "self"):
                yield node
                break


def _looked_up_model(call: ast.Call) -> str | None:
    """``<Model>.objects.filter(...)`` -> ``"Model"``, or None for anything else."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in LOOKUPS:
        return None
    manager = func.value
    if not isinstance(manager, ast.Attribute) or manager.attr not in MANAGERS:
        return None
    owner = manager.value
    return owner.id if isinstance(owner, ast.Name) else None


def _external_id_kwargs(call: ast.Call) -> list[str]:
    """Id keywords whose value is not a literal, so it came from outside."""
    return [
        keyword.arg for keyword in call.keywords
        if keyword.arg in ID_KWARGS and not isinstance(keyword.value, ast.Constant)
    ]


def audit_source(source: str, path: str, models: set[str]) -> set[str]:
    """The keys of every unnarrowed id lookup in one file.

    Narrowing is looked for across the whole *statement*, not just the call, so
    a queryset handed to a wrapper (``_branch_visible(request, Thing.objects
    .filter(...))``) counts as narrowed. That is how three of the apps here
    spell it, and reading only the call would flag every one of them.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    found = set()
    for handler in _request_handlers(tree):
        enclosing = {}
        for statement in ast.walk(handler):
            if isinstance(statement, ast.stmt):
                for child in ast.walk(statement):
                    enclosing.setdefault(id(child), statement)

        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            model = _looked_up_model(node)
            if model not in models or not _external_id_kwargs(node):
                continue
            statement = enclosing.get(id(node))
            segment = ast.get_source_segment(source, statement) or ""
            if any(narrower in segment for narrower in NARROWERS):
                continue
            found.add(f"{path}::{handler.name}::{model}")
    return found


def audit_tree() -> set[str]:
    """Run the audit over the application source."""
    models = branch_carrying_models()
    found = set()
    for dirpath, dirnames, filenames in os.walk(APPS_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = Path(dirpath) / name
            relative = _rel(path)
            if "tests" in relative.split(os.sep) or Path(name).stem.startswith("test"):
                continue
            found |= audit_source(
                path.read_text(encoding="utf-8"), relative, models,
            )
    return found


class BranchNarrowingAuditTests(SimpleTestCase):
    """The audit itself: it must see what it claims to see."""

    MODELS = {"Invoice"}

    def test_it_flags_an_id_lookup_a_request_handler_does_not_narrow(self):
        found = audit_source(
            "def get(self, request, pk):\n"
            "    return Invoice.objects.filter(entity=entity, pk=pk).first()\n",
            "made_up.py", self.MODELS,
        )

        self.assertEqual(found, {"made_up.py::get::Invoice"})

    def test_it_accepts_a_lookup_narrowed_in_the_same_statement(self):
        found = audit_source(
            "def get(self, request, pk):\n"
            "    return Invoice.objects.filter(\n"
            "        branch_q(request, include_shared=True), entity=entity, pk=pk,\n"
            "    ).first()\n",
            "made_up.py", self.MODELS,
        )

        self.assertEqual(found, set())

    def test_it_accepts_a_queryset_narrowed_by_a_wrapper(self):
        """Three apps spell it this way, and reading only the call would flag them."""
        found = audit_source(
            "def get(self, request, pk):\n"
            "    return _branch_visible(\n"
            "        request, Invoice.objects.filter(entity=entity, pk=pk),\n"
            "    ).first()\n",
            "made_up.py", self.MODELS,
        )

        self.assertEqual(found, set())

    def test_it_leaves_a_service_alone(self):
        """No request, no reachability decision: the caller was checked upstream."""
        found = audit_source(
            "def post_invoice(invoice_id):\n"
            "    return Invoice.objects.filter(pk=invoice_id).first()\n",
            "made_up.py", self.MODELS,
        )

        self.assertEqual(found, set())

    def test_it_leaves_a_literal_id_alone(self):
        found = audit_source(
            "def get(self, request):\n"
            "    return Invoice.objects.filter(pk=1).first()\n",
            "made_up.py", self.MODELS,
        )

        self.assertEqual(found, set())

    def test_it_reads_the_models_from_the_registry(self):
        """Not from a list somebody has to remember to extend.

        The three models the known defects were found on are the check: if the
        registry read ever breaks, this is what says so rather than the audit
        quietly finding nothing anywhere.
        """
        models = branch_carrying_models()

        for name in ("BankAccount", "ImportBatch", "ConfigurationValue"):
            self.assertIn(name, models)


class EveryBranchCarryingLookupIsAccountedForTests(SimpleTestCase):
    """The audit over the real source, against the two registries."""

    def test_no_unaccounted_lookup_exists(self):
        found = audit_tree()
        accounted = set(SETTLED_ELSEWHERE) | set(UNNARROWED)
        new = sorted(found - accounted)

        self.assertEqual(new, [], (
            "A request handler resolves a branch-carrying model by an id from "
            "outside and does not narrow to the caller's branches:\n  "
            + "\n  ".join(new)
            + "\n\nNarrow it with vs_rbac.scoping.branch_q, or add it to "
            "SETTLED_ELSEWHERE in this file saying what answers the branch "
            "question instead. If it is a real hole you are not fixing today, "
            "add it to UNNARROWED with what a caller could reach."
        ))

    def test_no_entry_outlives_the_lookup_it_describes(self):
        """A registry nobody prunes stops describing the code and starts hiding it."""
        found = audit_tree()
        accounted = set(SETTLED_ELSEWHERE) | set(UNNARROWED)
        stale = sorted(accounted - found)

        self.assertEqual(stale, [], (
            "These entries no longer match any lookup. Narrowed since, moved, "
            "or renamed - either way delete the entry:\n  " + "\n  ".join(stale)
        ))

    def test_the_audit_finds_something(self):
        """A walk that silently reads nothing passes both tests above.

        The commonest way for that to happen is APPS_ROOT pointing somewhere
        with no source under it, which no other assertion here would notice.
        """
        self.assertGreater(len(audit_tree()), 0)
