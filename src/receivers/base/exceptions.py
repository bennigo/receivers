"""Exception classes for receivers package."""


class ReceiverError(Exception):
    """Base exception for receiver-related errors."""
    pass


class ConnectionError(ReceiverError):
    """Exception raised when connection to receiver fails."""
    pass


class DownloadError(ReceiverError):
    """Exception raised when data download fails."""
    pass


class ConfigurationError(ReceiverError):
    """Exception raised when receiver configuration is invalid."""
    pass


class HealthCheckError(ReceiverError):
    """Exception raised when receiver health check fails."""
    pass


class AuthenticationError(ReceiverError):
    """Exception raised when authentication fails."""
    pass