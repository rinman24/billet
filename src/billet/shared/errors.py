"""Exception hierarchy for billet.

All billet-raised errors derive from :class:`BilletError` so the CLI can catch one
type and render a clean message instead of a traceback.
"""

from collections.abc import Sequence


class BilletError(Exception):
    """Base class for every billet error."""


class ConfigError(BilletError):
    """The config.toml is missing, malformed, or fails validation."""


class AzLoginRequired(BilletError):
    """The Azure CLI has no usable control-plane token; the operator must ``az login``."""


class HostOperationError(BilletError):
    """A host lifecycle operation cannot proceed from the current state."""


class ProcessError(BilletError):
    """An external command exited non-zero while the caller required success."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv: tuple[str, ...] = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        command = " ".join(self.argv)
        super().__init__(f"command failed (exit {returncode}): {command}\n{stderr}".rstrip())


class ProcessTimeoutError(ProcessError):
    """An external command was killed for exceeding its deadline.

    A killed process has no meaningful exit status, so ``returncode`` is the sentinel ``-1``
    and ``timeout`` (seconds) is what callers key on — never the message text.
    """

    def __init__(self, argv: Sequence[str], timeout: float) -> None:
        self.timeout = timeout
        super().__init__(argv, -1, f"timed out after {timeout:g}s")
