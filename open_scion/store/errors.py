"""Stable domain errors shared by repository, HTTP, and CLI layers."""


class ScionError(Exception):
    """A stable error that can be represented by both CLI and HTTP clients."""

    def __init__(self, message: str, code: str = "invalid", status: int = 400, details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details
