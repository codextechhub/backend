"""Domain exceptions for vs_workflow. All engine errors carry a typed error_code."""


class WorkflowError(Exception):
    error_code = "WORKFLOW_ERROR"
    default_message = "A workflow error occurred."
    http_status = 422

    def __init__(self, message=None, **kwargs):
        self.message = message or self.default_message
        self.extra = kwargs
        super().__init__(self.message)


class TemplateNotFoundError(WorkflowError):
    error_code = "TEMPLATE_NOT_FOUND"
    default_message = "No matching workflow template was found."
    http_status = 404


class TemplateInvalidError(WorkflowError):
    error_code = "TEMPLATE_INVALID"
    default_message = "The workflow template configuration is invalid."


class UnknownConditionFunctionError(WorkflowError):
    error_code = "UNKNOWN_CONDITION_FUNCTION"
    default_message = "A condition referenced an unregistered function key."


class UnknownOperatorError(WorkflowError):
    error_code = "UNKNOWN_OPERATOR"
    default_message = "A condition used an unsupported operator."


class UnknownDocumentTypeError(WorkflowError):
    error_code = "UNKNOWN_DOCUMENT_TYPE"
    default_message = "No handler is registered for this document type."


class InstanceNotFoundError(WorkflowError):
    error_code = "INSTANCE_NOT_FOUND"
    default_message = "Workflow instance not found."
    http_status = 404


class InvalidInstanceStateError(WorkflowError):
    error_code = "INVALID_INSTANCE_STATE"
    default_message = "This action cannot be performed on the instance in its current state."


class InstanceTerminalError(InvalidInstanceStateError):
    error_code = "INSTANCE_TERMINAL"
    default_message = "This workflow instance has already reached a terminal state."


class StageNotActiveError(WorkflowError):
    error_code = "STAGE_NOT_ACTIVE"
    default_message = "No stage is currently active on this instance."


class NotAnEligibleApproverError(WorkflowError):
    error_code = "NOT_ELIGIBLE_APPROVER"
    default_message = "You are not on the eligible approver list for this stage."
    http_status = 403


class RequesterCannotApproveError(WorkflowError):
    error_code = "REQUESTER_CANNOT_APPROVE"
    default_message = "The requester cannot approve their own submission."
    http_status = 403


class DuplicateApproverActionError(WorkflowError):
    error_code = "DUPLICATE_APPROVER_ACTION"
    default_message = "You have already recorded an action for the current attempt of this stage."
    http_status = 409


class ReversalNotAllowedError(WorkflowError):
    error_code = "REVERSAL_NOT_ALLOWED"
    default_message = "This action cannot be reversed."


class CancellationNotAllowedError(WorkflowError):
    error_code = "CANCELLATION_NOT_ALLOWED"
    default_message = "This instance cannot be cancelled."


class HandlerAlreadyRegisteredError(WorkflowError):
    error_code = "HANDLER_ALREADY_REGISTERED"
    default_message = "A handler is already registered for this document type."


class ConditionFunctionAlreadyRegisteredError(WorkflowError):
    error_code = "CONDITION_FUNCTION_ALREADY_REGISTERED"
    default_message = "A condition function is already registered under this key."
