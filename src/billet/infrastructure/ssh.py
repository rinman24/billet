"""SSH primitives: argv construction and operator egress-IP discovery.

Pure helpers over a :class:`ProcessRunner`; reachability polling and remote provisioning
(which involve retry/sleep policy) live in the access-layer provider, not here.
"""

from billet.infrastructure.process import ProcessRunner
from billet.shared.errors import HostOperationError

# Trust-on-first-use: accept an unknown host key, but still fail on a *changed* key.
_ACCEPT_NEW = ("-o", "StrictHostKeyChecking=accept-new")


def ssh_argv(
    user: str,
    host: str,
    remote_command: str | None = None,
    *,
    connect_timeout: int | None = None,
    batch_mode: bool = False,
) -> list[str]:
    """Build an ``ssh`` argv with trust-on-first-use host-key acceptance.

    The interactive flags (``-tt`` / ``-A``) the connect and agent-forwarded-clone paths
    need are deliberately omitted until the Workspace subsystem (slice 5) introduces them.
    """
    argv: list[str] = ["ssh", *_ACCEPT_NEW]
    if connect_timeout is not None:
        argv += ["-o", f"ConnectTimeout={connect_timeout}"]
    if batch_mode:
        argv += ["-o", "BatchMode=yes"]
    argv.append(f"{user}@{host}")
    if remote_command is not None:
        argv.append(remote_command)
    return argv


def operator_egress_ipv4(runner: ProcessRunner) -> str:
    """Return the operator's current public IPv4 (for pinning the inbound rule).

    Raises
    ------
    HostOperationError
        If the address could not be determined.
    """
    result = runner.run(["curl", "-4", "-s", "ifconfig.me"], check=False)
    ip = result.stdout.strip()
    if result.returncode != 0 or not ip:
        raise HostOperationError(
            "could not determine operator egress IPv4 (curl -4 ifconfig.me was empty)"
        )
    return ip
