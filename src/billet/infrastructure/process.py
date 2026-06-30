"""The subprocess seam: a ``ProcessRunner`` Protocol plus a real implementation.

``ProcessRunner`` is the single point where billet shells out. Tests substitute a fake
to spy on the exact argv passed to ``az`` / ``ssh`` without running anything.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import subprocess
from typing import Protocol

from billet.shared.errors import ProcessError


@dataclass(frozen=True, slots=True)
class CompletedProcess:
    """The outcome of running an external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    """Runs an external command. The seam tests substitute to spy on argv."""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> CompletedProcess:
        """Run ``argv``; raise :class:`ProcessError` on non-zero exit when ``check``."""
        ...


class SubprocessRunner:
    """A :class:`ProcessRunner` backed by :mod:`subprocess`."""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> CompletedProcess:
        """Run ``argv`` via :func:`subprocess.run`, capturing stdout/stderr as text."""
        # argv is always a list built by billet (never shell-interpolated user input).
        proc = subprocess.run(
            list(argv),
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
        result = CompletedProcess(
            argv=tuple(argv),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        if check and proc.returncode != 0:
            raise ProcessError(result.argv, result.returncode, result.stderr)
        return result
