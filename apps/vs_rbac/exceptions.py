"""Refusals this app raises that are not "you do not hold that permission".

A permission refusal is answered by DRF's own ``PermissionDenied`` and needs
nothing here. What does need its own class is the refusal that looks identical
to a caller and is the opposite problem: the role allows it, the plan does not.
"""


class PlanUpgradeRequired(Exception):
    """Raised when a school's plan does not reach what its role allows.

    403 rather than 402, because nothing about this request is a payment: the
    caller is authenticated, the school is live, and the school may well be
    fully paid up on a shallower tier. What they lack is depth.

    The message is the whole point of the class. It names the module, the
    depth the school reaches and the depth the feature sits at, so the person
    who reads it knows to talk to their proprietor rather than their school
    admin. See :mod:`vs_rbac.plan_gate` for why that difference matters.
    """

    error_code = "PLAN_UPGRADE_REQUIRED"
    default_message = (
        "This part of the product is not included in the school's current plan."
    )
    http_status = 403

    def __init__(self, message: str = "", *, extra=None):
        self.message = message or self.default_message
        self.extra = extra or {}
        super().__init__(self.message)


class RestrictedNeedsApprovalError(Exception):
    """A restricted permission would land on the actor's own access.

    Carried as a typed exception rather than a field error so the client can act
    on the code: the roles screen turns Save into "Raise for approval" and builds
    the request from ``restricted_additions``, which a sentence buried in a field
    error cannot be read from reliably.

    409 rather than 400 for the reason ``ApprovalNotConfiguredError`` uses it:
    the request is well formed, and it conflicts with a rule about who may
    decide, not with the shape of what was sent. ``http_status`` is the name the
    handler reads.
    """

    error_code = "RESTRICTED_NEEDS_APPROVAL"
    http_status = 409

    def __init__(self, message, *, restricted_additions):
        super().__init__(message)
        self.message = message
        self.extra = {"restricted_additions": sorted(restricted_additions)}
