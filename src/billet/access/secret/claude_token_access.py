"""ClaudeTokenAccess — resolve the central Claude Code OAuth token from a local command.

The operator authors ``[billet].claude_token_cmd`` as a shell command that prints the
long-lived ``CLAUDE_CODE_OAUTH_TOKEN`` to STDOUT (a Keychain / 1Password / Vault read, or
just ``printenv``). billet runs that command *locally* — on the operator's machine, exactly
where credential stores are unlocked — captures its STDOUT as the token, and threads the
value by function call into the container injection (see ADR-0006). This is the credential-
helper pattern git/docker/aws/kubectl use: the secret never lands in billet's config, and
rotation / revocation / audit stay in the store.

Security posture (see ADR-0006 §"token never in argv"):

- The command string is run *as-is* through the shell. billet interpolates **no** untrusted
  value (repo name, host, board data) into it — the operator owns every byte.
- Only STDOUT is read as the token; STDERR is surfaced to the operator (that is where
  ``op`` / ``vault`` / ``security`` print "not signed in / sealed" diagnostics).
- Any failure — non-zero exit, empty/whitespace-only STDOUT, or timeout — raises loudly and
  aborts ``start``. There is no fallback source and billet never proceeds with an empty
  token.
- The token value is never logged, echoed, or stored on a long-lived object.
"""

from billet.infrastructure.process import ProcessRunner
from billet.shared.errors import ConfigError, ProcessError

# A secret fetch is interactive-store-backed but must never hang `start` forever. The
# window is generous because the fetch can block on a biometric / master-password unlock
# (Touch ID, 1Password), yet still bounded so a wedged store cannot stall `start` forever.
_FETCH_TIMEOUT_SECONDS = 120.0


class ClaudeTokenAccess:
    """Resolves ``CLAUDE_CODE_OAUTH_TOKEN`` by running the operator's local fetch command."""

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner

    def resolve(self, command: str) -> str | None:
        """Run ``command`` locally and return its STDOUT token, or ``None`` when disabled.

        Empty / whitespace-only ``command`` means the feature is off: return ``None`` without
        running anything. Otherwise capture STDOUT, strip surrounding whitespace, and return
        the token — raising :class:`ConfigError` on any failure so ``start`` aborts.
        """
        if not command.strip():
            return None
        try:
            result = self._runner.run(
                ["sh", "-c", command],
                check=False,
                timeout=_FETCH_TIMEOUT_SECONDS,
            )
        except ProcessError as exc:
            # With check=False the only ProcessError the runner raises is the timeout kill.
            raise ConfigError(
                "claude_token_cmd timed out while fetching CLAUDE_CODE_OAUTH_TOKEN "
                f"(after {_FETCH_TIMEOUT_SECONDS:g}s). Is the credential store waiting on "
                "input?"
            ) from exc
        if result.returncode != 0:
            raise ConfigError(
                "claude_token_cmd failed while fetching CLAUDE_CODE_OAUTH_TOKEN "
                f"(exit {result.returncode}). Check that the credential store is unlocked / "
                f"signed in.\n{result.stderr.rstrip()}".rstrip()
            )
        token = result.stdout.strip()
        if not token:
            raise ConfigError(
                "claude_token_cmd produced no token on STDOUT. It must print the "
                "CLAUDE_CODE_OAUTH_TOKEN and nothing sensitive to STDERR.\n"
                f"{result.stderr.rstrip()}".rstrip()
            )
        return token
