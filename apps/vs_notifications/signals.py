# =============================================================================
# vs_notifications / signals.py
#
# Delivery signals fired when an EMAIL Notification reaches a terminal status.
#
# Two signals:
#   notification_sent    — an email notification transitioned to SENT.
#   notification_failed  — an email notification transitioned to FAILED
#                          (including pre-flight FAILED records created by
#                          dispatch, e.g. NO_EMAIL_ADDRESS / render failures).
#
# Both fire with a single keyword argument:
#   notification — the Notification instance in its terminal state.
#
# A later work package hooks vs_user invitation tracking onto these; the
# `notification.metadata` dict carries the correlation data (e.g. activation_key)
# the receiver needs. Receivers must be defensive — a signal handler must never
# break dispatch or the delivery task.
# =============================================================================

import django.dispatch

# Sender is always the Notification model; receivers connect with
# sender=Notification (or dispatch_uid) and read the `notification` kwarg.
# These are emitted only after the Notification row has reached its final email state.
notification_sent = django.dispatch.Signal()
notification_failed = django.dispatch.Signal()
