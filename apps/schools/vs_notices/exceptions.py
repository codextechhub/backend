"""Refusals the notices module raises.

Each carries a code the client branches on, a sentence written for the person
who pressed the button, and the HTTP status, and the shared exception handler
renders them as they are.
"""


class NoticesError(Exception):
    error_code = "NOTICES_ERROR"
    default_message = "That could not be done to this notice."
    http_status = 400

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


class AudienceRequired(NoticesError):
    error_code = "NOTICE_AUDIENCE_REQUIRED"
    default_message = "Choose who this notice is for before publishing it."
    http_status = 400


class AudienceOutOfReach(NoticesError):
    error_code = "NOTICE_AUDIENCE_OUT_OF_REACH"
    default_message = (
        "You can post only to the classes and branches you work with. "
        "Remove the ones outside your reach and try again."
    )
    http_status = 403


class NoticeNotEditable(NoticesError):
    error_code = "NOTICE_NOT_EDITABLE"
    default_message = "A withdrawn notice cannot be changed."
    http_status = 409


class NoticeNotPublishable(NoticesError):
    error_code = "NOTICE_NOT_PUBLISHABLE"
    default_message = "Only a draft can be published."
    http_status = 409


class NoticeNotWithdrawable(NoticesError):
    error_code = "NOTICE_NOT_WITHDRAWABLE"
    default_message = "Only a published notice can be withdrawn."
    http_status = 409
