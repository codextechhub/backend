"""Account lifecycle signals other modules hang their own consequences on.

Both signals exist for the same reason: ``vs_user`` is an engine app. It knows
about accounts, invitations and sessions, and nothing about schools, staff
records or employment. A module that owns a consequence connects here rather
than being imported from here.

``account_activated``
    Fires the moment an invited person uses their link and sets their first
    password, inside the same transaction that promotes the account, so a
    receiver's write commits with the activation or not at all. A school's
    staff record moves from Invited to Active when its owner accepts, and that
    is where it learns of it.

    Fires with one keyword argument:
      user - the :class:`~vs_user.models.User` whose account has just been
             activated.

    Receivers must be defensive about what they do NOT own, but they are inside
    the activation transaction on purpose: a receiver that must not fail
    activation should catch its own errors, and a receiver whose write must not
    be lost if activation rolls back should let them propagate.

``invitation_dispatch_settled``
    Fires once the invitation email has either been handed to the broker or
    refused by it, from inside the ``on_commit`` callback that does the handing
    over.

    It exists because the hand-off happens after the caller has already
    returned. Anything recording "the invitation went out" cannot know the
    outcome at the moment it writes, and a record written optimistically is one
    that keeps saying SENT through a broker outage in which no email was ever
    queued. Receivers write that state when the answer is known instead.

    Fires with three keyword arguments:
      invitation_id - primary key of the
                      :class:`~vs_user.models.UserInvitation`.
      user          - the account the invitation belongs to.
      accepted      - True when the broker took the job. False means no email
                      is on its way, and none will be without another dispatch.

    ``accepted=True`` says the job is queued, not that the message arrived.
    Delivery itself is reported by the vs_notifications signals that
    ``vs_user.receivers`` listens to.

    Receivers run outside any transaction and must never raise: the dispatch is
    already done by the time they are called and cannot be undone by a
    receiver failing.
"""
import django.dispatch

# Sender is always the User model; receivers connect with sender=User or a
# dispatch_uid and read the `user` kwarg.
account_activated = django.dispatch.Signal()

# Sender is always the UserInvitation model.
invitation_dispatch_settled = django.dispatch.Signal()
