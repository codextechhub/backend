"""Domain refusals for school and branch administration.

The project exception handler renders exceptions carrying ``error_code``,
``message`` and ``http_status`` into the standard API error envelope. Keeping
this refusal in the school app lets the provisioning service stay independent
of HTTP while every API caller receives the same explicit failure.
"""
from __future__ import annotations


class AdminProvisioningError(Exception):
    """A required administrator could not be made usable.

    School and branch creation wrap administrator provisioning in their own
    transactions. Letting this exception escape is what rolls those creation
    writes back instead of committing a record nobody can administer.
    """

    error_code = "ADMIN_PROVISIONING_FAILED"
    default_message = (
        "The required administrator could not be provisioned. Nothing was "
        "created; try again after the provisioning issue is resolved."
    )
    http_status = 503

    def __init__(self, message: str = ""):
        self.message = message or self.default_message
        self.extra = {}
        super().__init__(self.message)
