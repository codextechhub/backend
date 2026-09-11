"""What a Dynamic Role condition reads: the document, and who raised it.

Stage-owned rules read the document alone. A Dynamic Role also routes on the
requester - their role, their branch, the person themselves - so its
conditions read a small context instead::

    {
        "document": <the business document>,   # its own fields, by dotted path
        "document_type": "procurement.purchase_requisition",
        "amount": 150000000,                    # whole kobo, or None
        "branch": "12",                         # the request's branch, or None
        "requester": {"id": "...", "branch": "3", "role_keys": ["bursar"], ...},
    }

Ids are strings, so a value the screen wrote and one read from a model compare
equal. Requester facts beyond id, branch and roles come from the apps that own
them, registered with :func:`register_requester_facts`, because the engine may
not import those apps.
"""
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

RequesterFacts = Callable[[Any, Any], Dict[str, Any]]

_PROVIDERS: List[RequesterFacts] = []
_ENGINE_FACTS = frozenset({"id", "branch", "role_keys"})


def register_requester_facts(provider: RequesterFacts) -> RequesterFacts:
    """Add the facts a domain app can read about a requester.

    *provider(user, tenant)* returns a dict merged into ``requester``, or an
    empty one when it knows nothing about this person. It cannot replace the
    engine's own ``id``, ``branch`` or ``role_keys``. Registering the same
    provider again, as an app reload does, changes nothing.
    """
    if provider not in _PROVIDERS:
        _PROVIDERS.append(provider)
    return provider


def _as_id(value) -> Optional[str]:
    return None if value in (None, "") else str(value)


def document_amount(document) -> Any:
    """The amount a document routes on, in whole kobo, or None.

    Each money document names the column on its model as
    ``workflow_amount_field``; a document type without one has no amount.
    """
    if document is None:
        return None
    field_name = getattr(document, "workflow_amount_field", "")
    return getattr(document, field_name, None) if field_name else None


def requester_role_keys(user, tenant) -> List[str]:
    """Keys of the active roles *user* holds in *tenant*, at any branch."""
    from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment

    return sorted(set(
        TenantUserRoleAssignment.objects.filter(
            tenant=tenant,
            user=user,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            role__status=TenantRoleTemplate.Status.ACTIVE,
        ).values_list("role__key", flat=True)
    ))


def requester_facts(user, tenant) -> Dict[str, Any]:
    """Everything a condition may know about the person who raised a document.

    A provider that fails contributes nothing rather than stopping the
    activation. Its facts then match no condition and the document falls
    through to a later rule - at worst the Otherwise row - instead of the
    submission erroring out. The failure is logged.
    """
    if user is None:
        return {}
    facts: Dict[str, Any] = {
        "id": str(user.pk),
        "branch": _as_id(getattr(user, "branch_id", None)),
        "role_keys": requester_role_keys(user, tenant),
    }
    for provider in _PROVIDERS:
        try:
            extra = provider(user, tenant) or {}
        except Exception:  # noqa: BLE001 - a failing provider must not stop routing
            logger.exception("Requester facts provider %r failed.", provider)
            continue
        facts.update({key: value for key, value in extra.items() if key not in _ENGINE_FACTS})
    return facts


def build_rule_context(instance) -> Dict[str, Any]:
    """The context a Dynamic Role's rules read for a running instance."""
    document = instance.document
    return {
        "document": document,
        "document_type": instance.document_type,
        "amount": document_amount(document),
        "branch": _as_id(instance.branch_id),
        "requester": requester_facts(instance.requested_by, instance.tenant),
    }


def build_sample_context(*, requester, tenant, document_type: str = "",
                         sample: Optional[dict] = None) -> Dict[str, Any]:
    """A context for trying rules before any document exists.

    *sample* supplies what a document would: ``amount`` in kobo, ``branch``,
    ``document_type``, and ``document`` - a dict of the type's own fields. The
    requester's facts are read live, so the answer is the one the engine would
    give if that person raised it today. Without a sample branch, the
    requester's own branch stands in, as it does for the template preview.
    """
    sample = sample or {}
    return {
        "document": sample.get("document") or {},
        "document_type": sample.get("document_type") or document_type or "",
        "amount": sample.get("amount"),
        "branch": _as_id(sample.get("branch")) or _as_id(getattr(requester, "branch_id", None)),
        "requester": requester_facts(requester, tenant),
    }
