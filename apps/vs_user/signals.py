"""Account lifecycle signals other modules hang their own consequences on.

``account_activated`` fires the moment an invited person uses their link and
sets their first password, inside the same transaction that promotes the
account, so a receiver's write commits with the activation or not at all.

It exists so that domain modules can react without this app knowing they are
there. ``vs_user`` is an engine app: it knows about accounts, invitations and
sessions, and nothing about schools, staff records or employment. A school's
staff record moves from Invited to Active when its owner accepts, and the module
that owns that record connects here rather than being imported from here.

Fires with one keyword argument:
  user - the :class:`~vs_user.models.User` whose account has just been activated.

Receivers must be defensive about what they do NOT own, but they are inside the
activation transaction on purpose: a receiver that must not fail activation
should catch its own errors, and a receiver whose write must not be lost if
activation rolls back should let them propagate.
"""
import django.dispatch

# Sender is always the User model; receivers connect with sender=User or a
# dispatch_uid and read the `user` kwarg.
account_activated = django.dispatch.Signal()
