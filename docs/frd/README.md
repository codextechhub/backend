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

## Cover art and file size

Every document is generated from an earlier one, so its media is inherited
rather than authored. The cover photo used to arrive as a 1.28 MB JPEG, which
was 95% of a finished 1.37 MB document and made every version in this tree the
same size regardless of its contents.

`tools/shrink_cover_art.py` re-encodes oversized JPEG media in place, at 150 dpi
over the page. Both generators call it on the way out, so a new document is
small whichever reference it was built from, and nothing but the image bytes
changes. It is safe to run over the whole tree at any time:

    ./cx/bin/python docs/frd/tools/shrink_cover_art.py docs/frd --dry-run

A document that is already within budget is left alone, so repeated runs do not
put the cover through repeated lossy compression.
