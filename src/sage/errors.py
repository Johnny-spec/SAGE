"""Application errors with stable external meaning."""


class SageError(Exception):
    """Base application error."""


class TargetNotFoundError(SageError):
    """Raised when an explicitly selected Skill does not exist."""


class ExecutionError(SageError):
    """Raised when a Skill cannot be executed safely."""


class PolicyDeniedError(SageError):
    """Raised when caller policy denies or pauses an operation."""


class AttachmentNotFoundError(SageError):
    """Raised when an opaque attachment ID cannot be resolved."""


class AttachmentRejectedError(SageError):
    """Raised when an upload violates attachment policy."""
