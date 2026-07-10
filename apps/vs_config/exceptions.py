class ConfigurationError(Exception):
    error_code = "CONFIGURATION_ERROR"
    http_status = 422

    def __init__(self, message, *, extra=None):
        self.message = message
        self.extra = extra or {}
        super().__init__(message)


# Raised when a submitted value violates the definition type or validation rules.
class InvalidConfigurationValue(ConfigurationError):
    error_code = "INVALID_CONFIGURATION_VALUE"


# Raised when a value or override is written outside the definition allowed scope.
class InvalidConfigurationScope(ConfigurationError):
    error_code = "INVALID_CONFIGURATION_SCOPE"


# Raised when a school or platform tries to enable an unentitled capability.
class CapabilityNotEntitled(ConfigurationError):
    error_code = "CAPABILITY_NOT_ENTITLED"


# Raised when capability dependency evaluation detects an invalid graph.
class CapabilityDependencyError(ConfigurationError):
    error_code = "CAPABILITY_DEPENDENCY_ERROR"
