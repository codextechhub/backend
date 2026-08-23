"""Which product capability governs which permission module.

Two vocabularies exist on this platform and nothing joined them until now.

``vs_config.Capability`` is what a school BUYS - ``finance``, ``procurement``,
``students`` - the switchable units a package grants and an operator can toggle.
``vs_rbac.PermissionModule`` is what a permission is FILED UNDER - ``finance``,
``school``, ``academics`` - a bucket in the permission key's own namespace.

They were never the same list and were never meant to be. Two names line up by
coincidence (``finance``, ``procurement``); the rest do not. So a screen that
wants to say "these permissions belong to a module your school has not bought"
has to be told, and this is where it is told.

Read this file as three claims:

**Some permission modules are core.** Every school has them whatever it bought:
its own profile, its own roles, its own branches, onboarding, support tickets,
approvals, settings. They are absent from the maps below, and absence means
core - so a module added to the registry is treated as core until somebody
decides otherwise, which is the safe direction. The unsafe direction would be
defaulting to "not available" and quietly hiding a real permission.

**Some are governed at module level.** All of ``finance``'s permissions stand or
fall with the ``finance`` capability.

**One is governed per resource.** ``school`` holds the school's own core objects
AND its students and teachers, which are separately sellable. A module-level
answer for ``school`` would be wrong either way: mark it core and a school
without the students module is offered student permissions; mark it gated and
the same school loses access to its own branches and roles.

Capabilities with no permission module yet: ``attendance``, ``gradebook``,
``parents``, ``parent_portal``, ``student_portal``, ``vendors``. When those
modules get permissions, map them here rather than at the call site.
"""
from __future__ import annotations

#: Permission module -> the capability that must be on for it to be usable.
#: A module missing from here is core: available to every school.
CAPABILITY_BY_MODULE: dict[str, str] = {
    # Fees, invoices, payroll, the ledger.
    "finance": "finance",
    # Collections, payouts and virtual accounts are the finance product's
    # money-movement half; there is no separate capability for them.
    "payments": "finance",
    "procurement": "procurement",
    # Features rather than modules, but gated the same way.
    "communication": "email_alerts",
    "exports": "data_export",
    "import": "bulk_import",
}

#: (permission module, resource) -> capability, for the modules whose resources
#: do not all answer to the same product. Checked before CAPABILITY_BY_MODULE.
CAPABILITY_BY_RESOURCE: dict[tuple[str, str], str] = {
    ("school", "students"): "students",
    ("school", "teachers"): "teachers",
}


def capability_for(module: str, resource: str = "") -> str | None:
    """The capability governing one permission, or None when it is core.

    Resource first, so ``school.students.view`` answers to the students module
    while ``school.branches.view`` stays core.
    """
    specific = CAPABILITY_BY_RESOURCE.get((module, resource))
    if specific:
        return specific
    return CAPABILITY_BY_MODULE.get(module)
