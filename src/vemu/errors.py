"""Exception types used across v-EMU."""

from __future__ import annotations


class VemuError(Exception):
    """Base class for all expected v-EMU errors."""


class ValidationError(VemuError):
    """User input or state is invalid."""


class NotFound(VemuError):
    """A requested object does not exist."""


class Conflict(VemuError):
    """The requested operation conflicts with current state."""


class MutationError(VemuError):
    """A domain XML mutation could not be applied safely."""


class ExternalCommandError(VemuError):
    """An external command failed."""

    def __init__(self, command: str, returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr.strip()
        message = f"{command} failed with exit code {returncode}"
        if self.stderr:
            message += f": {self.stderr}"
        super().__init__(message)
