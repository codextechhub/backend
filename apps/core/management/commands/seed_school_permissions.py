"""Seed the school-scoped permission modules and attach prebuilt-role defaults.

This is the single source of truth for the school-facing permission keys used by
school-fe (the XVS school-facing app). It registers three modules - ``school``
(administration / people), ``academics`` (sessions / calendar / classes /
structure / subject), ``attendance`` (registers / corrections / reports) and
``gradebook`` (assessment setup / sheets / results / holds) - then attaches
sensible defaults to the ``school_admin`` / ``branch_admin`` / ``teacher`` /
``finance_admin`` PrebuiltRoleTemplates, and grows each school's copies of those
roles with the defaults the library role gains.

Run order::

    python manage.py seed_actions                    # canonical action verbs
    python manage.py seed_prebuilt_role_templates    # school_admin/branch_admin/teacher
    python manage.py seed_school_permissions

Two idempotent phases:
  1. Register modules + resources + permissions (with sensitivity per table).
     A resource usually arrives as a side effect of the first key naming it;
     one that carries Field Access fields and no keys is listed in
     ``FIELD_ONLY_RESOURCES`` and registered alongside them.
  2. Attach PrebuiltRolePermission defaults per the school_admin/branch_admin/
     teacher/finance_admin columns. ``finance_admin`` owns every finance and
     payments key by prefix (``seed_prebuilt_role_templates``); the few
     school-module keys it also needs, such as the gradebook's fee holds, are
     attached here. A default the library role gains on this run is granted
     to every school's copy of that role, its per-branch copies included
     (``branch_admin-<branch pk>``), at that moment and never again
     (:func:`vs_rbac.library_growth.attach_defaults`). A default the library
     already held is not offered again, so a key a school took off one of its
     roles stays off, and an explicit deny is never touched.

Safe to re-run. Supports ``--dry-run``.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


# Sensitivity levels (mirrors seed_platform_permissions).
_NORMAL, _SENSITIVE, _CRITICAL = "NORMAL", "SENSITIVE", "CRITICAL"

#: The tenants whose copies of the library roles grow with these defaults.
SCHOOL_TENANT_KIND = "SCHOOL"

# Prebuilt role keys the defaults attach to.
ROLE_SCHOOL_ADMIN = "school_admin"
ROLE_BRANCH_ADMIN = "branch_admin"
ROLE_TEACHER = "teacher"
ROLE_FINANCE_ADMIN = "finance_admin"
PREBUILT_ROLE_KEYS = [ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER, ROLE_FINANCE_ADMIN]

#: Keys registered unrestricted whatever their sensitivity.
#:
#: A SENSITIVE or CRITICAL key is otherwise restricted: it reaches a role the
#: editor holds only through a reviewed role change, never travels through a
#: permission group, and cannot be assigned by somebody who does not already
#: hold it.
#:
#: ``school.field_access.view`` is here because it only shows which switches a
#: role has. Seeing that the Storekeeper cannot read bank details opens nothing,
#: so it stays groupable beside ``school.roles.view``.
#:
#: ``school.field_access.update`` is deliberately absent. A switch opens a field
#: the moment it is saved, so an unrestricted update key would let somebody
#: holding only ``school.roles.update`` add it to their own role and then turn
#: on Read for a supplier's bank details with nobody approving either step.
#: Restricted, adding it to a role you hold goes through the role change ladder.
#: Both keys stay CRITICAL, which is what the audit queues read.
UNRESTRICTED_KEYS = frozenset({
    "school.field_access.view",
})

# ── Permission key table (single source of truth) ─────────────────────────────
# Each row: (dotted_key, description, sensitivity, {default-role flags}).
# The dotted key is module.resource.action. Modules: school, academics, attendance,
# gradebook.
# `defaults` lists which prebuilt roles receive this permission by default.
#
# This table MUST stay in lockstep with school-fe/src/permissions/index.ts.

# (module, resource, action, sensitivity, roles)
SCHOOL_PERMISSIONS: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    # module: school
    ("school", "dashboard", "view",            _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),

    ("school", "branches", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "branches", "create",           _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "branches", "update",           _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("school", "branches", "delete",           _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    # Finance Admin reads the roll because billing a class is choosing pupils
    # from its class list. A child's medical fields and the reasons behind a
    # status change stay closed to it: they are sensitive Field Access fields
    # and open only where a school turns the switch on.
    ("school", "students", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER, ROLE_FINANCE_ADMIN)),
    ("school", "students", "create",          _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "students", "update",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "students", "transition",       _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "students", "transfer",         _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "students", "suspend",          _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "students", "reactivate",       _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    # A child's blood group, allergies and conditions have no key of their own.
    # Who reads and corrects each of them is a Field Access switch on the role,
    # so a school can open the medical fields to its nurse without handing over
    # anything else on the record.
    # school_admin only for import: a bad import is the fastest way to
    # damage a school's records and is reversible only through the engine's
    # rollback. Export reaches branch_admin because a branch admin exports
    # their own branch's roll and cannot reach anybody else's.
    # Promotion is a bulk act on the whole roll, and it used to ride on
    # the broad student lifecycle key, the same key that transferred one child between
    # branches. One key could not sell the two at different depths.
    ("school", "students", "promote",          _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "students", "import",           _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "students", "export",           _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    # Moving a pupil to another branch carries their fee account and open
    # balance into the other branch's books. Both administrator roles hold it,
    # and the move itself needs a reach covering both branches, so a branch
    # administrator working at one branch holds the key and moves nobody.
    ("school", "students", "change_branch",    _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    # Giving a pupil or a guardian their own sign-in account, sending the
    # invitation again, and taking the account away. Its own resource because
    # it creates a login, a different decision from correcting the record;
    # scoped to the holder's branches like every other student key.
    ("school", "portal_access", "issue",       _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    # Notices to pupils. Administrators post to any audience inside their
    # branch reach; a teacher posts only to the classes they teach or lead,
    # under a key of its own so a school can give one without the other.
    ("school", "notices", "view",              _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("school", "notices", "post",              _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "class_notices", "post",        _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_TEACHER)),

    # Messaging. Taking part is for current staff: the inbox, writing, and the
    # staff end of conversations with families. A school leader may read a
    # group, a broadcast or a pupil's conversation without joining it; each
    # opening is recorded. See docs/messaging/messaging-design.md.
    ("school", "messages", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER, ROLE_FINANCE_ADMIN)),
    ("school", "messages", "send",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER, ROLE_FINANCE_ADMIN)),
    ("school", "messages", "export",           _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "family_messages", "send",      _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("school", "family_messages", "office",    _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "message_groups", "create",     _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "message_broadcasts", "send",   _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "message_groups", "inspect",    _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "message_broadcasts", "inspect", _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "message_welfare", "inspect",   _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    # The staff directory and every staff record, read as the role allows. A
    # teacher does not hold it: a teacher reads a colleague's profile as far as
    # the school's staff profile policy shows colleagues and line managers
    # (vs_staff services/visibility.py), and their own through the same policy.
    # Holding this key would read every colleague at the branch in full,
    # whatever the school chose to show.
    ("school", "teachers", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "teachers", "create",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "teachers", "update",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "teachers", "transition",       _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    # Who teaches which subject to which class, and who is a class teacher.
    # SENSITIVE because it decides who owns a class's marks, and branch_admin
    # holds it because staffing a branch's classes is a branch decision.
    ("school", "teachers", "assign",           _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    # Loading the staff list from a spreadsheet. Its own key, and SENSITIVE,
    # exactly as school.students.import is: one upload creates accounts, role
    # grants and invitations for everybody in the file, so it is not the same
    # act as adding one person through the form.
    #
    # branch_admin holds it, where the student import stops at school_admin.
    # The difference is what the two uploads touch: a staff file adds people to
    # the branch the uploader already staffs one at a time through
    # school.teachers.create, while a roll import rewrites records belonging to
    # children across the school. A branch opening with forty teachers is the
    # create key at the scale a spreadsheet exists for.
    #
    # The resource is ``staff`` rather than another verb on ``teachers``. The
    # register keys keep their name because school-fe and four tables point at
    # it; this one is minted here and says what it governs.
    ("school", "staff", "import",              _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    # A member of staff's qualifications, certificates and documents. Split
    # from the register because reading a directory and reading somebody's
    # certificates are sold at different depths, and one key served both.
    ("school", "staff_records", "view",        _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "staff_records", "update",      _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    # Leave is three keys rather than two because applying and deciding are
    # different acts. Every member of staff applies for their own; reading a
    # colleague's is not something a teacher may do, which is why ``view`` stops
    # at the two admin roles. Approving is not here at all: it is the workflow
    # engine's, held through its own keys, so no one key both files an absence
    # and approves it.
    ("school", "leave", "apply",               _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("school", "leave", "view",                _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "leave", "update",              _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "leave", "cancel",              _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    # The school's own org chart. Every member of staff reads it, so a teacher
    # holds view: knowing who you report to is not an administrator's secret.
    # Drawing it and appointing people to it stay with the two admin roles, and
    # a branch admin's writes reach their own branch's units and posts only.
    # All NORMAL: nothing here grants anybody a permission, and the dated
    # history, the vacancies and the summary are read with the register's
    # school.teachers.update rather than with any key of this resource.
    ("school", "organogram", "view",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("school", "organogram", "create",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "organogram", "update",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "organogram", "delete",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "organogram", "assign",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    ("school", "administrators", "view",       _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "administrators", "create",     _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "administrators", "update",     _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "administrators", "suspend",    _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "administrators", "reactivate", _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    # People asking to delete their own sign-in. Both administrator roles read
    # the requests of their reach and decide them, because a request that waits
    # for a decision with nobody holding the key to make it would leave a pupil
    # with a request that is never answered. Deciding closes an account, so it
    # is SENSITIVE; reading who asked is NORMAL.
    ("school", "account_closures", "view",     _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "account_closures", "update",   _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    # The school's own fee rules: when a bill falls due and whether pupils may
    # pay their own fees. They sit in Finance Settings beside the finance
    # console's own sections, so the role that runs the money holds both.
    ("school", "fees", "view",                 _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_FINANCE_ADMIN)),
    ("school", "fees", "update",               _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_FINANCE_ADMIN)),

    ("school", "settings", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "settings", "update",           _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    # The school's own identity record: ownership type, term structure,
    # currency, address, website, motto, registration id and logo. NOT its
    # name, address slug or code - those are allocated by CodeX at creation and
    # stay on the platform endpoint, which is why this is a separate resource
    # from ``settings`` rather than another action on it.
    #
    # ``.update`` is school_admin only. A branch admin may read the profile
    # (the currency and term structure govern screens they use) but changing
    # the school's identity is not a branch-level decision.
    ("school", "profile", "view",              _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("school", "profile", "update",            _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("school", "roles", "view",                _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("school", "roles", "assign",              _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    # Explicit grants replace the removed implicit SCHOOL_ADMIN persona
    # authority over role templates (tenant refactor: personas grant nothing).
    ("school", "roles", "create",              _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "roles", "update",              _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("school", "roles", "approve",             _CRITICAL,  (ROLE_SCHOOL_ADMIN,)),
    ("school", "roles", "delete",              _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    # Field Access: which roles read and write each registered field of a
    # record. Both CRITICAL because a switch can open a child's medical record
    # or a supplier's bank details. View is unrestricted and update is
    # restricted: see UNRESTRICTED_KEYS.
    ("school", "field_access", "view",         _CRITICAL,  (ROLE_SCHOOL_ADMIN,)),
    ("school", "field_access", "update",       _CRITICAL,  (ROLE_SCHOOL_ADMIN,)),

    # School-scoped proxy (impersonation). Deliberately a SEPARATE namespace
    # from platform.impersonation.* - a school role must never carry a key that
    # the platform tiering would read as cross-tenant reach. Holding
    # school.impersonation.start lets an actor proxy ANY active user in their
    # OWN tenant (self excluded); cross-tenant is impossible by construction.
    # CRITICAL + restricted: this is the most powerful key a school can hold,
    # so it is school_admin-only by default (never branch_admin/teacher).
    # Per-user permission exceptions (overrides) on school user profiles.
    # CRITICAL + restricted: an ALLOW override hands a single person a key their
    # role does not carry, so this is school_admin-only by default. `.view` is
    # equally restricted on purpose - without it a user must never be able to
    # learn that exceptions exist on their own account.
    ("school", "user_overrides", "view",       _CRITICAL,  (ROLE_SCHOOL_ADMIN,)),
    ("school", "user_overrides", "create",     _CRITICAL,  (ROLE_SCHOOL_ADMIN,)),
    ("school", "user_overrides", "delete",     _CRITICAL,  (ROLE_SCHOOL_ADMIN,)),

    ("school", "impersonation", "start",       _CRITICAL,  (ROLE_SCHOOL_ADMIN,)),
    ("school", "impersonation", "end",         _CRITICAL,  (ROLE_SCHOOL_ADMIN,)),
    ("school", "impersonation", "view",        _CRITICAL,  (ROLE_SCHOOL_ADMIN,)),

    # module: academics
    # Finance Admin reads sessions and classes for the same reason it reads
    # the roll: a fee structure is linked to a term, and a class is billed
    # from its list.
    ("academics", "session", "view",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER, ROLE_FINANCE_ADMIN)),
    ("academics", "session", "create",         _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("academics", "session", "update",         _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("academics", "session", "activate",       _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("academics", "session", "archive",        _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("academics", "session", "delete",         _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("academics", "calendar", "view",          _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "calendar", "create",        _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "calendar", "update",        _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "calendar", "delete",        _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    ("academics", "classes", "view",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER, ROLE_FINANCE_ADMIN)),
    ("academics", "classes", "create",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "classes", "update",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "classes", "archive",        _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "classes", "reactivate",     _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "classes", "assign",         _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    # Who takes which non-core subject. Every teacher holds both keys, but the
    # save is judged class by class: only the class's own class teacher or a
    # holder of academics.classes.assign may change a class's choices, so
    # holding update opens no class a teacher does not lead.
    ("academics", "electives", "view",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "electives", "update",       _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),

    # Academic structure. "structure" covers departments, programs and
    # levels, which are one screen and one mental object to a school; "subject"
    # is its own resource because a branch admin may create one and may not
    # create a programme. Sessions and classes reuse the resources above.
    ("academics", "structure", "view",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "structure", "create",       _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("academics", "structure", "update",       _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
    ("academics", "structure", "archive",      _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("academics", "structure", "reactivate",   _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    # Its own key, and SENSITIVE, exactly as school.students.import is. One
    # upload builds a school's whole spine - every programme, year group and
    # class - so it is not the same act as adding one class through the form,
    # and a branch admin who may create a class may not rebuild the school.
    ("academics", "structure", "import",       _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    # A resource of its own rather than four more uses of the calendar
    # keys: adding a public holiday and rebuilding the school's entire timetable
    # are not one act. Merging them would also hand academics.calendar.delete,
    # which is SENSITIVE and school_admin-only, the power to delete a grid.
    ("academics", "timetable", "view",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "timetable", "create",       _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "timetable", "update",       _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "timetable", "delete",       _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    # branch_admin holds publish so a branch's grid does not wait on the head
    # office. A school wanting exam publication reserved to head office
    # withholds it in a role template of its own, which is configuration.
    ("academics", "timetable", "publish",      _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    # Exams carry their own keys. They used to ride on the timetable's, which
    # meant exam scheduling and the weekly class timetable could never be sold
    # at different depths: banding the timetable key one way gave exams away,
    # and the other way took the class timetable with it.
    ("academics", "exam", "view",              _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "exam", "create",            _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "exam", "update",            _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "exam", "delete",            _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("academics", "exam", "publish",           _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),

    ("academics", "subject", "view",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("academics", "subject", "create",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "subject", "update",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("academics", "subject", "archive",        _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("academics", "subject", "reactivate",     _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    # module: attendance
    #
    # ``mark`` and ``mark_any`` are two keys because RBAC has no "only my own
    # class" scope: ``mark`` takes the register of the class the holder is
    # class teacher of, ``mark_any`` takes any class in the branches the holder
    # can see (a registrar taking a class on a teacher's behalf). The own-class
    # rule is the register service's, not a key's.
    ("attendance", "register", "view",         _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("attendance", "register", "mark",         _NORMAL,    (ROLE_TEACHER,)),
    ("attendance", "register", "mark_any",     _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("attendance", "register", "remind",       _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("attendance", "correction", "create",     _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    # Deciding a correction rewrites a day the school has closed, so it is
    # SENSITIVE and held by the two administrator roles only.
    ("attendance", "correction", "approve",    _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("attendance", "staff", "view",            _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("attendance", "staff", "mark",            _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("attendance", "report", "view",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    # Carries a roll of children and their absences out of the platform, so it
    # is SENSITIVE exactly as school.students.export is.
    ("attendance", "report", "export",         _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("attendance", "settings", "update",       _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),

    # module: gradebook
    #
    # A key answers whether somebody may open a screen or take an action at
    # all; which sheet or class it applies to is a rule in the services. The
    # main teacher of a subject enters its scores with ``sheet.enter``, and
    # ``sheet.enter_any`` enters any sheet in the holder's branches (a school
    # grants it; no role holds it by default). Moderating is the head of the
    # subject's department and compiling a class is its class teacher, both by
    # rule. Publishing results, putting a child's result on hold and changing
    # assessment setup are SENSITIVE: they decide what a family is given.
    ("gradebook", "hub", "view",              _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("gradebook", "sheet", "view",            _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN, ROLE_TEACHER)),
    ("gradebook", "sheet", "enter",           _NORMAL,    (ROLE_TEACHER,)),
    ("gradebook", "sheet", "enter_any",       _SENSITIVE, ()),
    ("gradebook", "sheet", "remind",          _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("gradebook", "moderation", "approve",    _NORMAL,    (ROLE_TEACHER,)),
    ("gradebook", "class", "submit",          _NORMAL,    (ROLE_TEACHER,)),
    ("gradebook", "class", "publish",         _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    ("gradebook", "hold", "view",             _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_FINANCE_ADMIN)),
    ("gradebook", "hold", "update",           _SENSITIVE, (ROLE_FINANCE_ADMIN,)),
    ("gradebook", "release", "view",          _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("gradebook", "result", "view",           _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("gradebook", "setup", "view",            _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("gradebook", "setup", "update",          _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    # Correcting a result after it was published. Raising one asks the principal
    # to change what a family was given, so it is open to both administrator
    # roles; deciding one changes it, so it is SENSITIVE and the school
    # administrator's alone (the raiser may never decide their own).
    ("gradebook", "amendment", "create",      _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("gradebook", "amendment", "approve",     _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
    # Uploading a spreadsheet of marks. Which sheet is still the main teacher's
    # by rule, or an ``enter_any`` holder's.
    ("gradebook", "sheet", "import",          _NORMAL,    (ROLE_TEACHER, ROLE_SCHOOL_ADMIN)),
    # Averages, pass rates and movement for the whole class list. Exporting
    # carries a table of named children's results out of the platform, so it is
    # SENSITIVE exactly as the attendance and student exports are.
    ("gradebook", "analytics", "view",        _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
    ("gradebook", "analytics", "export",      _SENSITIVE, (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
]

#: Resources that carry Field Access fields and no permission keys.
#:
#: A resource is otherwise created as a side effect of the first key naming it,
#: so a resource with no keys would never exist, and ``sync_field_registry``
#: refuses a field declaration whose resource is missing. Guardians are exactly
#: that case: a guardian's record is read with ``school.students.view`` and
#: corrected with ``school.students.update``, so minting guardian keys would
#: hand every school a second set of switches governing nothing, while the
#: contact details on the record still need somewhere to hang their own.
#:
#: The module must already be in ``MODULES``; the label and description come
#: from the same two tables every other resource reads.
FIELD_ONLY_RESOURCES: list[tuple[str, str]] = [
    ("school", "guardians"),
    # A lesson register is read with attendance.register.view and marked with
    # attendance.register.mark, so it needs no keys of its own; its mark, time
    # and note are fields a school restricts per role.
    ("attendance", "lesson"),
    # A department is created and edited with the academic structure keys; who
    # heads it is a field a school restricts per role.
    ("academics", "department"),
]

#: Readable names for resources whose slug reads wrongly on screen.
#:
#: A resource with no entry here reads as its slug, title-cased, which is what
#: ``display_label`` does and what most of them want.
RESOURCE_LABELS: dict[tuple[str, str], str] = {
    ("school", "guardians"): "Guardians",
    ("school", "portal_access"): "Pupil portal access",
    ("school", "account_closures"): "Account deletion requests",
    ("school", "notices"): "Notices",
    ("school", "class_notices"): "Class notices",
    ("school", "messages"): "Messages",
    ("school", "family_messages"): "Messages with families",
    ("school", "message_groups"): "Staff message groups",
    ("school", "message_broadcasts"): "Staff broadcasts",
    ("school", "message_welfare"): "Pupil conversations (welfare oversight)",
    ("academics", "electives"): "Who takes what",
    ("academics", "department"): "Departments",
    # The keys say ``teachers`` because ``Permission.key`` is a primary key
    # four tables point at. The register they guard is the whole staff list,
    # and the personal details hanging off this resource belong to the bursar
    # and the registrar as much as to the teacher.
    ("school", "teachers"): "Staff",
}

MODULES: dict[str, str] = {
    "school": "School administration - branches, people, fees, settings, roles.",
    "academics": "Academic operations - sessions, calendar, timetables, classes, structure, subjects.",
    "attendance": "Attendance - daily class registers, corrections, reminders, staff registers, reports and settings.",
    "gradebook": "Gradebook and assessments - assessment setup, score sheets, moderation, results, fee holds, report cards, amendments and analytics.",
}

# Short descriptions per resource (for PermissionResource rows).
RESOURCE_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("school", "dashboard"):      "School overview dashboard",
    ("school", "branches"):       "School branch management",
    ("school", "students"):       "Student records",
    ("school", "portal_access"):  "Pupils' and guardians' own sign-in accounts for the pupil and parent portals",
    ("school", "notices"):        "Notices the school posts to its pupils",
    ("school", "class_notices"):  "Notices a teacher posts to the classes they teach",
    ("school", "messages"):       "Conversations between staff: the inbox, writing, and downloading a conversation",
    ("school", "family_messages"): "The staff end of conversations with parents and pupils, and being the school office",
    ("school", "message_groups"): "Staff group chats: making one, and reading any in reach read-only",
    ("school", "message_broadcasts"): "One-to-many messages to staff: sending one, and reading any in reach read-only",
    ("school", "message_welfare"): "Reading a pupil's conversation with staff read-only, with every opening recorded",
    # Fields only: a guardian is read and corrected with the student keys.
    ("school", "guardians"):      "Parents and guardians, and the details the school reaches them on",
    # "Staff" rather than "Teacher": this resource governs the bursar and the
    # registrar as much as the teacher. The KEY stays school.teachers.* because
    # Permission.key is a primary key that four tables point at and school-fe
    # checks by name; the description is a sentence.
    ("school", "teachers"):       "Staff records",
    ("school", "staff"):          "Loading the staff list in bulk from a spreadsheet",
    ("school", "staff_records"):  "Staff qualifications, certificates and documents",
    ("school", "leave"):          "Staff leave requests",
    ("school", "organogram"):     "The school's org chart: units, posts, appointments and reporting lines",
    ("school", "administrators"): "School administrator accounts",
    ("school", "account_closures"): "People asking to delete their own sign-in account: reading the requests and closing or declining them",
    ("school", "fees"):           "Fees and billing",
    ("school", "settings"):       "School settings",
    ("school", "profile"):        "The school's own identity record (ownership, term structure, currency, branding)",
    ("school", "roles"):          "School role management",
    ("school", "field_access"):   "Which roles read and write each registered field of a record",
    ("school", "user_overrides"): "Per-user permission exceptions on school user profiles",
    ("school", "impersonation"):  "School-scoped proxy (impersonate a user in your own school)",
    ("academics", "session"):     "Academic sessions",
    ("academics", "calendar"):    "Academic calendar",
    ("academics", "classes"):     "Classes",
    ("academics", "electives"):   "Which students take each non-core subject in a class",
    # Fields only: a department is read and changed with the structure keys.
    ("academics", "department"):  "Departments, and the member of staff who heads each",
    ("academics", "structure"):   "Academic structure - departments, programs and levels",
    ("academics", "subject"):     "Subjects and the levels they are offered at",
    ("academics", "timetable"):   "Rooms, the bell schedule and class timetables",
    ("academics", "exam"):        "Exams, exam slots and their publication",
    ("attendance", "register"):   "Daily class registers: reading them, taking them and reminding a class teacher",
    ("attendance", "correction"): "Corrections to a mark after its day has closed",
    # Fields only: a lesson register is read and marked with the register keys.
    ("attendance", "lesson"):     "Lesson registers, and the details a lesson mark carries",
    ("attendance", "staff"):      "The staff attendance register",
    ("attendance", "report"):     "Attendance summaries, the chronic-absence list and their exports",
    ("attendance", "settings"):   "Attendance settings and the school's reasons for excusing an absence",
    ("gradebook", "hub"):         "The results hub: which sheets are in and which are late",
    ("gradebook", "sheet"):       "Score sheets: reading them, entering scores, uploading them from a spreadsheet and reminding a main teacher",
    ("gradebook", "moderation"):  "Moderating a score sheet as the head of the subject's department",
    ("gradebook", "class"):       "Compiling a class's results, submitting them and publishing them",
    ("gradebook", "hold"):        "Fee holds on a student's result",
    ("gradebook", "release"):     "The release list and the report cards of a class",
    ("gradebook", "result"):      "A student's results across terms",
    ("gradebook", "setup"):       "Assessment setup: structures, grading scales, report card options and remark templates",
    ("gradebook", "amendment"):   "Corrections to a published result: raising one and deciding it",
    ("gradebook", "analytics"):   "Performance analytics over published results, and their exports",
}


class Command(BaseCommand):
    help = (
        "Seed the school + academics + attendance + gradebook permission modules, attach prebuilt-role "
        "defaults, and grant newly attached defaults to schools' copies (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without touching the DB.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            # Wrap the whole thing in an atomic block we roll back so counts are
            # accurate against the real DB without persisting anything.
            try:
                with transaction.atomic():
                    self._run(dry_run=True)
                    raise _DryRunRollback()
            except _DryRunRollback:
                self.stdout.write(self.style.WARNING(
                    "\n  [dry-run] All changes rolled back. Nothing was written.\n"
                ))
        else:
            with transaction.atomic():
                self._run(dry_run=False)

    def _resource(self, module, name, cache):
        """The resource row for ``module.name``, created if it is new.

        The description is a creation default, so a corrected one is never
        written over. The readable label is filled in whenever it is blank,
        because a resource created before labels existed carries none and would
        otherwise read as its slug on the permission and Field Access screens
        for good.
        """
        from vs_rbac.models import PermissionResource

        rkey = (module.name, name)
        resource = cache.get(rkey)
        if resource is not None:
            return resource
        label = RESOURCE_LABELS.get(rkey, "")
        resource, _ = PermissionResource.objects.get_or_create(
            module=module,
            name=name,
            defaults={
                "label": label,
                "description": RESOURCE_DESCRIPTIONS.get(rkey, name),
                "is_active": True,
            },
        )
        if label and not resource.label:
            resource.label = label
            resource.save(update_fields=["label", "updated_at"])
        cache[rkey] = resource
        return resource

    def _run(self, dry_run: bool):
        from vs_rbac.library_growth import attach_defaults
        from vs_rbac.models import (
            Permission,
            PermissionAction,
            PermissionModule,
            PermissionResource,
            PrebuiltRoleTemplate,
            PermissionScope,
        )

        prefix = "  [dry-run]" if dry_run else " "

        # ── Phase 1: register modules, resources, permissions ─────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 1 - registering school + academics + attendance + gradebook permissions...\n"
        ))

        modules: dict[str, PermissionModule] = {}
        for module_name, module_desc in MODULES.items():
            module, created = PermissionModule.objects.get_or_create(
                name=module_name,
                defaults={"description": module_desc, "is_active": True},
            )
            modules[module_name] = module
            if created:
                self.stdout.write(f"{prefix} + module: {module_name}")

        resources: dict[tuple[str, str], PermissionResource] = {}
        created_perm_count = 0
        all_keys: list[str] = []

        for module_name, resource_name, action_name, sensitivity, _roles in SCHOOL_PERMISSIONS:
            module = modules[module_name]

            resource = self._resource(module, resource_name, resources)

            action = PermissionAction.objects.filter(name=action_name).first()
            if not action:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  Action '{action_name}' not found - run seed_actions first. "
                    f"Skipping {module_name}.{resource_name}.{action_name}."
                ))
                continue

            expected_key = f"{module_name}.{resource_name}.{action_name}"
            all_keys.append(expected_key)

            is_restricted = (
                sensitivity in (_SENSITIVE, _CRITICAL)
                and expected_key not in UNRESTRICTED_KEYS
            )
            perm = Permission.objects.filter(key=expected_key).first()
            if perm:
                self.stdout.write(f"    {expected_key} (exists)")
            else:
                perm = Permission(
                    module=module,
                    resource=resource,
                    action=action,
                    description="",
                    is_restricted=is_restricted,
                    sensitivity_level=sensitivity,
                    is_active=True,
                    scope=PermissionScope.TENANT,
                )
                perm.save()
                created_perm_count += 1
                self.stdout.write(f"{prefix} + {perm.key}")

        # Resources that carry Field Access fields and no keys. Registered the
        # same way and in the same phase as the rest, because the field sync
        # refuses a declaration whose resource is missing.
        for module_name, resource_name in FIELD_ONLY_RESOURCES:
            self._resource(modules[module_name], resource_name, resources)

        # ── Phase 2: attach prebuilt-role defaults ────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n  Phase 2 - attaching prebuilt-role defaults...\n"
        ))

        # Build role -> [keys] from the table.
        role_default_keys: dict[str, list[str]] = {k: [] for k in PREBUILT_ROLE_KEYS}
        for module_name, resource_name, action_name, _sensitivity, roles in SCHOOL_PERMISSIONS:
            key = f"{module_name}.{resource_name}.{action_name}"
            for role_key in roles:
                role_default_keys[role_key].append(key)

        total_grown = 0
        for role_key in PREBUILT_ROLE_KEYS:
            role = PrebuiltRoleTemplate.objects.filter(key=role_key).first()
            if role is None:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  Prebuilt role '{role_key}' not found - run "
                    f"seed_prebuilt_role_templates first. Skipping its defaults."
                ))
                continue

            attached, grown = attach_defaults(
                role, role_default_keys[role_key],
                branch_copies=True, tenant_kind=SCHOOL_TENANT_KIND,
            )
            total_grown += grown
            self.stdout.write(
                self.style.SUCCESS(
                    f"{prefix} {role_key}: attached {len(attached)} new default(s) "
                    f"({len(role_default_keys[role_key])} total), "
                    f"{grown} grant(s) added to schools' copies."
                )
                if attached else
                f"{prefix} {role_key}: all {len(role_default_keys[role_key])} defaults already attached."
            )

        # ── Summary ───────────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {created_perm_count} new permission(s) created, "
            f"{len(all_keys)} school/academics/attendance/gradebook keys registered; "
            f"{total_grown} grant(s) added to schools' copies of the library roles.\n"
        ))


class _DryRunRollback(Exception):
    """Internal sentinel to roll back the transaction in --dry-run mode."""
