class ConfigurationError(Exception):
    error_code = "CONFIGURATION_ERROR"
    http_status = 422

    def __init__(self, message, *, extra=None):
        self.message = message
        self.extra = extra or {}
        super().__init__(message)


class InvalidConfigurationValue(ConfigurationError):
    error_code = "INVALID_CONFIGURATION_VALUE"


class InvalidConfigurationScope(ConfigurationError):
    error_code = "INVALID_CONFIGURATION_SCOPE"


class CapabilityNotEntitled(ConfigurationError):
    error_code = "CAPABILITY_NOT_ENTITLED"


class CapabilityDependencyError(ConfigurationError):
    error_code = "CAPABILITY_DEPENDENCY_ERROR"
