# XVS Requirements Documents

This folder separates the two XVS requirements document families.

## Module Requirements Document (MRD)

The MRD is the cross-module product tracker. It records all modules, backend and
integration states, capability coverage, current gaps, priorities, and build
order.

Location: `module-requirements/`

Current naming: `XVS_Module_Requirements_Document_v<version>.docx`

Historical files retain their original `XVS_Module_FR_Breakdown_*` names so the
revision trail remains intact.

## Functional Requirements Document (FRD)

An FRD is the detailed, testable functional contract for one module. It records
requirements, actors and permissions, workflows, lifecycle rules, data and API
contracts, dependencies, current gaps, and MRD traceability.

Location: `functional-requirements/<module>/`

Current naming:
`XVS_M<module-number>_<module-name>_Functional_Requirements_Document_v<version>.docx`

Only School and Branch Management currently has an FRD. A new module FRD must not
be created without an explicit user request.

## Versioning

MRD and FRD versions advance independently. A completed backend change may
require an MRD update, one or more existing FRD updates, both, or neither. Prior
versions are preserved and every generated document must be rendered and reviewed
before commit.
