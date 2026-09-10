"""Give a staff record to the people at a school who have an account but none.

The directory lists the people who hold a ``StaffProfile``, which is the
narrowing that keeps a school's parents out of its staff list. Two groups of
people were left on the wrong side of it, both of them staff by any reading:

* **Every administrator a school was created with.** School and branch primary
  admins are provisioned by ``vs_schools.services.admin_provisioning``, which
  wrote the account, the grant and the invitation and no record. The head
  teacher who runs a branch could not be given a class, could not file leave,
  was missing from her own branch roster and could not be found by the search
  box - while the checklist card above the empty directory read as done,
  because it counts accounts.
* **Everybody invited before this module existed.** ``me/staff/`` used to list
  every user the tenant owned. When it moved here it started listing records
  instead, and a school's existing teachers and bursars silently left the page
  they had always been on.

What it will not do:

* **It does not guess who is staff.** Only accounts holding an ACTIVE role
  grant at a school get a record, because a grant is the school's own statement
  that this person does a job here. Parents are what the narrowing exists to
  keep out and they hold no grant, so nothing here has to reason about them -
  and the mother who teaches at her daughter's school is not excluded for having
  a guardian row, because within one school she is one account and one of the
  two things she is is a teacher.
* **It does not invent an employment history.** Where a record starts is read
  off the account with the same rule new records use, so somebody who never
  accepted their invitation reads Invited and somebody signing in daily reads
  Active. The first event carries the date the account was created rather than
  the date this runs, because that is when the person joined the school.
* **It does not reverse.** Employment history is the thing a school is asked
  for years later, and deleting records on the way down would discard the ones
  normal traffic has written since. Going back leaves them in place.
"""
from django.db import migrations
from django.utils import timezone

from schools.vs_staff.constants import UNACCEPTED_ACCOUNT_STATUSES

#: Accounts that were never a person doing a job: a parked record and a refused
#: creation. Everyone else with a grant is somebody the school gave work to.
NOT_PEOPLE_YET = ("DRAFT", "REJECTED")


def _job_titles(apps):
    """``(tenant_id, email)`` to the title the school typed on the admin link.

    "IT Head", "Head Teacher" - the school's own words for the posting, which
    exist for its administrators and for nobody else. A record with no title is
    perfectly ordinary, so an address that matches nothing here is left blank.
    """
    titles = {}
    school_link = apps.get_model("vs_schools", "SchoolPrimaryAdmin")
    branch_link = apps.get_model("vs_schools", "BranchPrimaryAdmin")

    for link in school_link.objects.select_related("contact", "school"):
        titles[(link.school.tenant_id, link.contact.email.lower())] = link.school_role
    for link in branch_link.objects.select_related("contact", "branch"):
        titles[(link.branch.tenant_id, link.contact.email.lower())] = link.branch_role
    return titles


def write_missing_records(apps, schema_editor):
    User = apps.get_model("vs_user", "User")
    StaffProfile = apps.get_model("vs_staff", "StaffProfile")
    StaffEmploymentEvent = apps.get_model("vs_staff", "StaffEmploymentEvent")

    titles = _job_titles(apps)

    candidates = (
        User.objects
        .filter(
            # Spelled out rather than read off the model: a historical model
            # carries its columns and none of its choice classes.
            tenant__kind="SCHOOL",
            tenant_role_assignments__assignment_status="ACTIVE",
        )
        .exclude(status__in=NOT_PEOPLE_YET)
        .exclude(pk__in=StaffProfile.objects.values("user_id"))
        .select_related("tenant")
        .distinct()
    )

    for user in candidates.iterator():
        status = "INVITED" if user.status in UNACCEPTED_ACCOUNT_STATUSES else "ACTIVE"
        joined = timezone.localtime(user.created_at)
        profile = StaffProfile.objects.create(
            tenant_id=user.tenant_id,
            user=user,
            # Their account's own branch, which is what a posting means and is
            # already null for somebody who works across the whole school.
            branch_id=user.branch_id,
            job_title=titles.get((user.tenant_id, user.email.lower()), ""),
            employment_status=status,
            # So the directory's newest-first order puts these people where they
            # joined rather than all at the top on the day this runs.
            created_at=user.created_at,
        )
        StaffEmploymentEvent.objects.create(
            tenant_id=user.tenant_id,
            staff=profile,
            from_status="",
            to_status=status,
            effective_date=joined.date(),
            reason="Added to the staff list",
            created_at=user.created_at,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("vs_staff", "0004_alter_teachingassignment_part"),
        ("vs_schools", "0012_a_tier_sets_depth_and_stops_capping_size"),
        ("vs_rbac", "0019_ticket_assignment_is_the_desks_own"),
        ("vs_tenants", "0010_remove_branch__type"),
        ("vs_user", "0011_user_card_login_id"),
    ]

    operations = [
        migrations.RunPython(write_missing_records, migrations.RunPython.noop),
    ]
