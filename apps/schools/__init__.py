"""XVS - the schools product, built on the domain-neutral platform engines.

Everything school-shaped lives under this package: school and branch setup,
the academic structure, the school year, students and guardians, staff, the
portals, and the FAL (the finance abstraction layer, which is school-centric
by design and therefore belongs here rather than in ``core``).

The direction of dependency is one-way and load-bearing:

    apps/schools/*   ->   vs_finance, vs_procurement, vs_payments, vs_rbac,
                          vs_workflow, vs_notifications, vs_audit, core

The engines never import anything under here. They know about entities,
customers, invoices, vendors, roles and approvals; they do not know what a
student or a term is. Where a school fact has to reach an engine, it goes
through the FAL, not through an import. See ``CLAUDE.md`` and
``docs/architecture/school-decoupling-scope.md``.

App labels are preserved across the move into this package (see each app's
``AppConfig.label``), so ``vs_schools`` remains the label and
``vs_schools_*`` remain the table names. Only the Python import path moved:
``vs_schools.models`` is now ``schools.vs_schools.models``.
"""
