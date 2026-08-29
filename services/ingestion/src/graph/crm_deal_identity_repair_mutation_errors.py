"""Errors raised by atomic CRM-deal repair mutation transactions."""


class RepairMutationDriftError(RuntimeError):
    """Raised before commit when immutable request or frozen evidence differs."""


class RepairMutationAuthorityError(RuntimeError):
    """Raised when run, unit, fence, control, source, or reconstruction authority is absent."""
