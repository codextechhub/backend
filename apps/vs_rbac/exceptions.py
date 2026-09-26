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


class PrebuiltRoleMissing(LookupError):
    """A role the product ships is not in the prebuilt library.

    The library is reference data an install seeds once, and a tenant's copy of
    a role is made from it. An install that is missing a row cannot provision
    that role for anybody, and answering the caller with ``None`` made the
    absence read exactly like success: a school came out with two roles where
    the product ships five, and nobody was told.

    The keys are carried on the exception so a caller provisioning several at
    once can name every missing one in a single refusal, rather than sending an
    operator round the loop once per role.
    """

    def __init__(self, keys):
        self.keys = tuple(keys)
        super().__init__(
            "The prebuilt role library has no active template for: "
            f"{', '.join(self.keys)}. Run seed_prebuilt_role_templates."
        )


class SharedRecordReadOnly(Exception):
    """A branch-bound caller changing a row shared beyond their branches.

    403 rather than 404: the caller can already read the row, so refusing to
    name it would hide nothing. The message names who can make the change,
    because "forbidden" alone sends a branch administrator looking for a
    permission they will never be given.
    """

    error_code = "SHARED_RECORD_READ_ONLY"
    default_message = (
        "This is shared across more than your branch, so only a school-wide "
        "administrator can change it."
    )
    http_status = 403

    def __init__(self, message: str = "", *, extra=None):
        self.message = message or self.default_message
        self.extra = extra or {}
        super().__init__(self.message)

