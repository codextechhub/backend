"""Refusals the pupil portal raises, each written for the pupil reading it."""


class PortalError(Exception):
    error_code = "PORTAL_ERROR"
    default_message = "That could not be done."
    http_status = 400

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


class ProfileFieldInvalid(PortalError):
    """A contact detail the pupil typed that cannot be stored as typed."""

    error_code = "PROFILE_FIELD_INVALID"
    http_status = 400


class PhotoSelfEditOff(PortalError):
    error_code = "PHOTO_SELF_EDIT_OFF"
    default_message = (
        "Your school changes student photographs itself. Ask the school office "
        "if yours needs updating."
    )
    http_status = 403
