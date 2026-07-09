"""ComposeContainerAccess — read devcontainer.json and drive compose over SSH.

Implements ``ContainerAccess``. It reads each repo's ``.devcontainer/devcontainer.json``
on the Host (a read-only data contract — see ADR-0002) into :class:`DevcontainerFacts`, and
runs ``docker compose`` on the Host over SSH. It mirrors the ``remote_build_and_bootstrap``
/ ``remote_compose_up`` phases of the lifted ``up.sh``, with the repo-owned in-container
bootstrap sourced from the devcontainer's ``postCreateCommand`` rather than a bespoke config
value. The operator's *personal* bootstrap is the one phase that does not go through
``docker compose exec``: it hops through the container's loopback sshd with the agent
forwarded, so it can use the operator's git identity (see ``run_personal_bootstrap``).

Compose runs as plain ``docker compose`` (no ``sg docker`` wrapper): a fresh SSH session on
a provisioned Host already has the ``docker`` group active, so the wrapper the cold path
once needed is unnecessary here.
"""

import posixpath
import shlex
from typing import Any, cast

from billet.contracts import DevcontainerFacts, RemoteHost, WorkspaceSpec
from billet.infrastructure import ssh
from billet.infrastructure.process import ProcessRunner
from billet.shared import jsonc
from billet.shared.errors import ConfigError, HostOperationError

_DEVCONTAINER_REL = ".devcontainer/devcontainer.json"
_DEVCONTAINER_DIR = ".devcontainer"

# Bound connection establishment only (never command runtime): a deallocated Azure host
# drops inbound packets, so an untimed probe would hang `billet ls` indefinitely.
_SSH_CONNECT_TIMEOUT = 5

# ssh(1) reserves exit 255 for its own failures (connect/auth); remote commands never
# produce it, so it cleanly separates "host unreachable" from "command failed on host".
_SSH_TRANSPORT_RC = 255


def _as_str_list(value: Any, what: str) -> list[str]:
    """Coerce a JSON string-or-array value to a list of strings, or raise."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        items: list[str] = []
        for item in cast("list[object]", value):
            if not isinstance(item, str):
                raise ConfigError(f"devcontainer.json: '{what}' entries must be strings")
            items.append(item)
        return items
    raise ConfigError(f"devcontainer.json: '{what}' must be a string or array of strings")


def _normalize_compose_files(value: Any) -> tuple[str, ...]:
    """Normalize ``dockerComposeFile`` (str or list) to repo-root-relative paths.

    devcontainer.json declares the path(s) relative to the ``.devcontainer/`` folder; compose
    is invoked from the repo root, so each is re-rooted under ``.devcontainer/``.
    """
    raw = _as_str_list(value, "dockerComposeFile")
    if not raw:
        raise ConfigError("devcontainer.json: 'dockerComposeFile' is empty")
    return tuple(posixpath.normpath(posixpath.join(_DEVCONTAINER_DIR, item)) for item in raw)


def _normalize_post_create(value: Any) -> str | None:
    """Normalize ``postCreateCommand`` (str / list / absent) to a single shell string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return shlex.join(_as_str_list(value, "postCreateCommand"))
    raise ConfigError(
        "devcontainer.json: object form of 'postCreateCommand' is not yet supported; "
        "use a string or array"
    )


class ComposeContainerAccess:
    """A ``ContainerAccess`` over ``docker compose`` on the Host via SSH."""

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner

    # --- reading the data contract -------------------------------------------------

    def read_facts(self, spec: WorkspaceSpec, remote: RemoteHost) -> DevcontainerFacts:
        """Read + parse ``<repo_dir>/.devcontainer/devcontainer.json`` on the host."""
        path = posixpath.join(spec.repo_dir, _DEVCONTAINER_REL)
        argv = ssh.ssh_argv(
            remote.admin_user,
            remote.ip,
            f"cat {shlex.quote(path)}",
            connect_timeout=_SSH_CONNECT_TIMEOUT,
            batch_mode=True,
        )
        result = self._runner.run(argv, check=False)
        _assert_transport_ok(result.returncode, remote)
        if result.returncode != 0:
            raise ConfigError(
                f"could not read {path} on {remote.ip} — is the repo cloned? "
                "Run `billet start` to clone it first."
            )
        return self._facts_from_json(result.stdout, path)

    @staticmethod
    def _facts_from_json(text: str, path: str) -> DevcontainerFacts:
        try:
            data = jsonc.loads(text)
        except ValueError as exc:
            raise ConfigError(f"invalid devcontainer.json at {path}: {exc}") from exc
        if "dockerComposeFile" not in data:
            raise ConfigError(f"{path}: missing 'dockerComposeFile' (billet drives compose)")
        for key in ("service", "workspaceFolder", "remoteUser"):
            if not isinstance(data.get(key), str):
                raise ConfigError(f"{path}: missing or non-string '{key}'")
        return DevcontainerFacts(
            service=data["service"],
            compose_files=_normalize_compose_files(data["dockerComposeFile"]),
            workspace_folder=data["workspaceFolder"],
            remote_user=data["remoteUser"],
            post_create_command=_normalize_post_create(data.get("postCreateCommand")),
        )

    # --- driving the stack ---------------------------------------------------------

    def compose_up(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> None:
        """Run the host bootstrap hook, write the agent-teams flag, then ``up -d --build``."""
        self._run_script(remote, self._compose_up_script(spec, facts))

    def run_post_create(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
    ) -> None:
        """Run the devcontainer ``postCreateCommand`` in the service container (if any)."""
        if facts.post_create_command is None:
            return
        script = self._exec_script(spec, facts, facts.post_create_command)
        self._run_script(remote, script)

    def run_personal_bootstrap(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts, command: str
    ) -> None:
        """Run the operator's ``personal_bootstrap_cmd`` in the service container (if set).

        Unlike the repo-owned ``postCreateCommand``, the personal bootstrap routinely needs
        the operator's git identity (e.g. cloning a private dotfiles repo), and a
        ``docker compose exec`` session inherits no agent socket. So this phase hops through
        the container's loopback sshd (ADR-0003) with the agent forwarded end-to-end
        (operator -> Host -> container) — like the clone, the key is never parked.
        """
        if not command:
            return
        argv = _script_argv(remote, forward_agent=True)
        self._runner.run(argv, input_text=self._personal_bootstrap_script(spec, facts, command))

    def verify(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> None:
        """Run the Workspace's ``verify_cmd`` in the service container."""
        self._run_script(remote, self._exec_script(spec, facts, spec.verify_cmd))

    def compose_stop(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
    ) -> None:
        """Stop the compose stack (non-destructive — named volumes/data persist)."""
        self._run_script(remote, _prelude(spec) + _compose_cmd(facts, "stop") + "\n")

    def is_running(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> bool:
        """Return whether the service container is currently running."""
        ps = _compose_cmd(facts, "ps", "--status", "running", "-q", shlex.quote(facts.service))
        script = _prelude(spec) + ps + "\n"
        argv = _script_argv(remote)
        result = self._runner.run(argv, input_text=script, check=False)
        _assert_transport_ok(result.returncode, remote)
        return bool(result.stdout.strip())

    # --- helpers -------------------------------------------------------------------

    def _run_script(self, remote: RemoteHost, script: str) -> None:
        self._runner.run(_script_argv(remote), input_text=script)

    @staticmethod
    def _compose_up_script(spec: WorkspaceSpec, facts: DevcontainerFacts) -> str:
        prelude = (
            _prelude(spec)
            + f"HOST_BOOTSTRAP_CMD={shlex.quote(spec.host_bootstrap_cmd)}\n"
            + 'eval "$HOST_BOOTSTRAP_CMD"\n'
            + f"AGENT_TEAMS_FLAG={shlex.quote(spec.agent_teams_flag)}\n"
        )
        # Write the optional Claude agent-teams flag once (orchestrator-side, never tracked).
        agent_teams = (
            'if [ -n "$AGENT_TEAMS_FLAG" ] && [ ! -f .claude/settings.local.json ]; then\n'
            "  mkdir -p .claude\n"
            "  cat > .claude/settings.local.json <<JSON\n"
            "{\n"
            '  "env": {\n'
            '    "$AGENT_TEAMS_FLAG": "1"\n'
            "  }\n"
            "}\n"
            "JSON\n"
            "fi\n"
        )
        return prelude + agent_teams + _compose_cmd(facts, "up", "-d", "--build") + "\n"

    @staticmethod
    def _exec_script(spec: WorkspaceSpec, facts: DevcontainerFacts, command: str) -> str:
        exec_cmd = _compose_cmd(
            facts, "exec", "-T", shlex.quote(facts.service), "bash", "-lc", shlex.quote(command)
        )
        return _prelude(spec) + exec_cmd + "\n"

    @staticmethod
    def _personal_bootstrap_script(
        spec: WorkspaceSpec, facts: DevcontainerFacts, command: str
    ) -> str:
        """Build the Host-side script: agent-forwarded ssh into the container's sshd.

        ``-n`` keeps the inner ssh off the outer script's stdin. Host-key checking is
        disabled for this hop only: the container regenerates host keys on rebuild and the
        connection never leaves the VM's loopback. ``command`` is double-quoted because two
        shells consume a layer each — the Host bash parsing this script, then the
        container-side sshd shell evaluating the remote command — leaving the ``cd … &&``
        line as the single ``bash -lc`` argument in the container.
        """
        inner = f"cd {shlex.quote(facts.workspace_folder)} && {command}"
        hop = (
            "exec ssh -n -A"
            " -o BatchMode=yes"
            " -o StrictHostKeyChecking=no"
            " -o UserKnownHostsFile=/dev/null"
            " -o LogLevel=ERROR"
            " -o ConnectionAttempts=5"
            f" -p {spec.container_ssh_port}"
            f" {shlex.quote(facts.remote_user)}@127.0.0.1"
            f" bash -lc {shlex.quote(shlex.quote(inner))}"
        )
        return "set -euo pipefail\n" + hop + "\n"


def _script_argv(remote: RemoteHost, *, forward_agent: bool = False) -> list[str]:
    """Build the ``ssh … bash -se`` argv every remote script runs through."""
    return ssh.ssh_argv(
        remote.admin_user,
        remote.ip,
        "bash -se",
        connect_timeout=_SSH_CONNECT_TIMEOUT,
        batch_mode=True,
        forward_agent=forward_agent,
    )


def _assert_transport_ok(returncode: int, remote: RemoteHost) -> None:
    """Raise ``HostOperationError`` when ssh itself failed rather than the remote command."""
    if returncode == _SSH_TRANSPORT_RC:
        raise HostOperationError(
            f"could not reach {remote.ip} over SSH — is the Host up? "
            "Run `billet host up` to start it."
        )


def _prelude(spec: WorkspaceSpec) -> str:
    """Shared remote-script header: fail-fast, cd into the repo, export the assigned port.

    ``BILLET_CONTAINER_SSH_PORT`` is exported before every ``docker compose`` invocation so
    the repo's compose can bind its sshd to billet's assigned loopback port
    (``127.0.0.1:${BILLET_CONTAINER_SSH_PORT:-2222}:22``). See ADR-0003.
    """
    return (
        "set -euo pipefail\n"
        f"cd {shlex.quote(spec.repo_dir)}\n"
        f"export BILLET_CONTAINER_SSH_PORT={spec.container_ssh_port}\n"
    )


def _compose_cmd(facts: DevcontainerFacts, *args: str) -> str:
    """Build a ``docker compose -f … <args>`` command with each compose file quoted."""
    files = " ".join(f"-f {shlex.quote(path)}" for path in facts.compose_files)
    return f"docker compose {files} {' '.join(args)}".strip()
