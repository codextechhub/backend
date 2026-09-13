"""Take the lockout out of the status column and leave it in the lockout row.

A brute-force lockout was written in two places: ``AccountLockout.locked_until``,
which expires by itself when the configured window passes, and ``User.status``,
which never does. The moment the window closed the two disagreed, and the status
was the one the sign-in gate read last, so the fifteen minutes a school
configured lasted until an administrator or a password reset moved the column by
hand.

The code now writes only the lockout row. This clears the column of a value it
should never have carried, so that nobody is left refused by a condition that
has already ended. Leaving a row at LOCKED is the one outcome that is not
available: nothing writes that value any more, so such a row would be refused
for ever by a condition that can never clear - the defect itself, preserved for
the unluckiest accounts.

What each row becomes
---------------------
The status an account held before it was locked is recorded nowhere, so it is
read back from the evidence the rest of the tables still carry.

**Usable before it was locked** - ``is_active`` true. The derivation
deliberately left that flag alone for a locked account, precisely so that
clearing the lockout could restore the account as it was, which makes it a
fossil of the status underneath. These go back to ACTIVE. It is the ordinary
case and very nearly all of them.

**Invited and never used** - four marks together, and the account goes back to
PENDING so its invitation works again:

* it has an invitation row at all. One is created only by
  ``UserCreationService.finalize_invitation``, when a hire has been approved and
  the email is sent, so a draft, a hire still awaiting approval and a rejected
  hire have none. That is what keeps this branch away from them: a rejected hire
  must never be handed an activation link, and this rule cannot reach one.
* the invitation is unused. Activating consumes it, and so does withdrawing it,
  so a used row means the account was either taken up or deliberately closed.
* ``last_login_at`` is null. Nobody has ever completed a sign-in as this person.
* ``password_changed_at`` is null and the stored password is unusable. No
  password has ever been set, by activation, by a reset or by a change.

**Everything else** - SUSPENDED. An account that had a password or had signed
in, and one that was never invited at all, are each indistinguishable from the
others of their kind once LOCKED has been written over them. SUSPENDED is the
state that means "an administrator has to decide": it grants nothing, it is
visible on every account screen, and reactivating it is one action. Guessing
ACTIVE here would give a working sign-in back to somebody who was closed out.

What is still a guess
---------------------
One shape is not distinguishable and comes back as PENDING: an account
**deactivated after it was invited but before it activated**. It carries all
four marks above, because nothing in these columns records a deactivation, so
its invitation link works again for whatever is left of its expiry window.

The exposure is bounded rather than dismissed. The link needs the token that was
emailed at the time, it dies on the school's configured expiry, and PENDING
grants nothing except the ability to set a first password. An administrator who
sees the account back among the invited can withdraw the invitation, which
closes it immediately. The alternative - sending every unclear row to SUSPENDED
- would strand a genuinely invited teacher behind an administrator before she
could ever use the link her school sent her, which is the commoner case by far.

A row whose window has not passed yet is moved like the rest, and stays refused
for exactly as long as it has left, by ``locked_until``. After this migration no
user row carries LOCKED, and none is written again.

The lockout rows themselves are untouched. A ``locked_until`` in the past
refuses nobody, and the failure count and the address it came from are the
evidence a school reads after an incident.

Reversibility
-------------
There is no reverse. Nothing records which of these rows carried LOCKED a moment
ago, and a reverse that re-locked accounts by guessing would restore the defect
rather than the data. It is written as a no-op so that migrating backwards past
this point stays possible.
"""

from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX
from django.db import migrations


def clear_the_locked_status(apps, schema_editor):
    User = apps.get_model("vs_user", "User")

    locked = User.objects.filter(status="LOCKED")

    # Usable before it was locked, on the evidence of the preserved flag.
    locked.filter(is_active=True).update(status="ACTIVE")

    # Invited and never used. Every clause is load-bearing; see the docstring.
    invited = list(
        locked.filter(
            is_active=False,
            invitation__isnull=False,
            invitation__is_used=False,
            last_login_at__isnull=True,
            password_changed_at__isnull=True,
            password__startswith=UNUSABLE_PASSWORD_PREFIX,
        ).values_list("pk", flat=True)
    )
    User.objects.filter(pk__in=invited).update(status="PENDING")

    # Whatever the columns cannot tell apart. Re-read, so the rows already
    # moved above are no longer among them.
    locked.filter(is_active=False).update(status="SUSPENDED")


class Migration(migrations.Migration):

    dependencies = [
        ("vs_user", "0011_user_card_login_id"),
    ]

    operations = [
        migrations.RunPython(
            clear_the_locked_status,
            migrations.RunPython.noop,
        ),
    ]
