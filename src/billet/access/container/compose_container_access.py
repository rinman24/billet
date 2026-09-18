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
from billet.infrastructure.process import OnLine, ProcessRunner
from billet.shared import jsonc
from billet.shared.errors import ConfigError, HostOperationError, ProcessError

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
    """A ``ContainerAccess`` over ``docker compose`` on the Host via SSH.

    ``on_compose_line`` (optional, wired by the composition root) streams the output of
    the one multi-minute phase — ``compose_up`` — line by line so the client can render
    a live log tail. Every other operation stays buffered.
    """

    def __init__(self, runner: ProcessRunner, on_compose_line: OnLine | None = None) -> None:
        self._runner = runner
        self._on_compose_line = on_compose_line

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

    def compose_up(
        self,
        spec: WorkspaceSpec,
        remote: RemoteHost,
        facts: DevcontainerFacts,
        claude_oauth_token: str | None = None,
    ) -> None:
        """Run the host hook, write the agent-teams flag, ``up -d --build``, inject the token.

        When ``claude_oauth_token`` is set, a python3 read-merge-write runs inside the service
        container (after the build) to merge ``CLAUDE_CODE_OAUTH_TOKEN`` into the container
        user's ``~/.claude/settings.json`` ``env`` block (ADR-0006). ``None``/empty skips it —
        no exec, no settings write. The token travels only as the exec'd python3's STDIN
        (embedded in a quoted heredoc), never as any argv on the Mac, the Host, or the
        container. The same exec first re-owns a root-owned, empty ``~/.claude`` to the
        login user (ADR-0013 §6): ``up -d`` does not wait for the Berth entrypoint, so on a
        cold start the injection can reach the Locker before the entrypoint's own repair.
        """
        self._run_script(
            remote,
            self._compose_up_script(spec, remote, facts, claude_oauth_token),
            on_line=self._on_compose_line,
        )

    def run_post_create(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
    ) -> None:
        """Run the devcontainer ``postCreateCommand`` in the service container (if any)."""
        if facts.post_create_command is None:
            return
        script = self._exec_script(spec, remote, facts, facts.post_create_command)
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

    def verify(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> str:
        """Run the Workspace's ``verify_cmd`` in the service container; return its output.

        Collected through the streaming seam rather than the buffered one so stdout and
        stderr come back interleaved in the order the command actually printed them — a
        buffered run captures the two separately, and concatenating them would reorder a
        version banner relative to the warning that followed it. Nothing is streamed live:
        the sink is a local list, and the joined text is handed to the caller to render.

        A failing command is re-raised carrying that same merged text rather than the bare
        stderr it exited with: a ``verify_cmd`` is typically a test or build runner, which
        reports its verdict on *stdout*, so the default ``ProcessError`` view would show an
        empty tail for the one failure the operator most needs to read.
        """
        lines: list[str] = []
        try:
            self._run_script(
                remote,
                self._exec_script(spec, remote, facts, spec.verify_cmd),
                on_line=lines.append,
            )
        except ProcessError as exc:
            raise ProcessError(exc.argv, exc.returncode, "\n".join(lines) or exc.stderr) from exc
        return "\n".join(lines)

    def compose_stop(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
    ) -> None:
        """Stop the compose stack (non-destructive — named volumes/data persist)."""
        self._run_script(remote, _prelude(spec, remote) + _compose_cmd(facts, "stop") + "\n")

    def is_running(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> bool:
        """Return whether the service container is currently running."""
        ps = _compose_cmd(facts, "ps", "--status", "running", "-q", shlex.quote(facts.service))
        script = _prelude(spec, remote) + ps + "\n"
        argv = _script_argv(remote)
        result = self._runner.run(argv, input_text=script, check=False)
        _assert_transport_ok(result.returncode, remote)
        return bool(result.stdout.strip())

    # --- helpers -------------------------------------------------------------------

    def _run_script(self, remote: RemoteHost, script: str, on_line: OnLine | None = None) -> None:
        self._runner.run(_script_argv(remote), input_text=script, on_line=on_line)

    @staticmethod
    def _compose_up_script(
        spec: WorkspaceSpec,
        remote: RemoteHost,
        facts: DevcontainerFacts,
        claude_oauth_token: str | None = None,
    ) -> str:
        prelude = (
            _prelude(spec, remote)
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
        build = _compose_cmd(facts, "up", "-d", "--build") + "\n"
        inject = _claude_token_injection(facts, claude_oauth_token) if claude_oauth_token else ""
        return prelude + agent_teams + build + inject

    @staticmethod
    def _exec_script(
        spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts, command: str
    ) -> str:
        exec_cmd = _compose_cmd(
            facts, "exec", "-T", shlex.quote(facts.service), "bash", "-lc", shlex.quote(command)
        )
        return _prelude(spec, remote) + exec_cmd + "\n"

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


def _prelude(spec: WorkspaceSpec, remote: RemoteHost) -> str:
    """Shared remote-script header: fail-fast, cd into the repo, export billet's variables.

    Both ``BILLET_*`` variables the Workspace templates interpolate are exported before
    every ``docker compose`` invocation, so the repo's compose needs no ``.env`` on the Host.
    ``BILLET_CONTAINER_SSH_PORT`` lets the compose bind its sshd to billet's assigned
    loopback port (``127.0.0.1:${BILLET_CONTAINER_SSH_PORT:-2222}:22``, ADR-0003).
    ``BILLET_AUTHORIZED_KEYS`` names the Host admin user's ``authorized_keys`` so the
    container's sshd trusts the same key that opens the Host
    (``${BILLET_AUTHORIZED_KEYS:-./authorized_keys-stub}``). The admin user is the one this
    script already ssh's in as (``RemoteHost.admin_user``) — no new lookup. A shell export
    outranks compose's ``.env`` interpolation, which is what makes a stale ``.env`` left on
    a Host inert.
    """
    return (
        "set -euo pipefail\n"
        f"cd {shlex.quote(spec.repo_dir)}\n"
        f"export BILLET_CONTAINER_SSH_PORT={spec.container_ssh_port}\n"
        f"export BILLET_AUTHORIZED_KEYS={shlex.quote(_authorized_keys_path(remote))}\n"
    )


def _authorized_keys_path(remote: RemoteHost) -> str:
    """Build the Host admin user's ``authorized_keys`` path — the file the container's sshd trusts."""
    return posixpath.join("/home", remote.admin_user, ".ssh", "authorized_keys")


def _compose_cmd(facts: DevcontainerFacts, *args: str) -> str:
    """Build a ``docker compose -f … <args>`` command with each compose file quoted."""
    files = " ".join(f"-f {shlex.quote(path)}" for path in facts.compose_files)
    return f"docker compose {files} {' '.join(args)}".strip()


# The heredoc delimiter that carries the merge program (and the token literal) as STDIN.
# Quoted (``<<'PYEOF'``) so the Host shell performs no expansion on the body — the token
# passes through verbatim to the container's python3, never touched by the shell.
_PYEOF = "BILLET_CLAUDE_TOKEN_PY"

# The read-merge-write program, minus its leading literal ``token`` assignment, which is
# prepended per-call via ``repr()`` so arbitrary token bytes are a valid, self-escaping
# Python string literal. It never prints the token — only the path.
#
# Home resolution: ``Path.home()`` honours ``$HOME`` when set and otherwise falls back to
# the current uid's passwd entry — so under ``docker compose exec -u <remote_user>`` it
# resolves to that user's ``~`` via the uid, while a test can point it at a temp dir by
# setting ``HOME``. The same program is therefore correct in the container and executable
# in a unit test.
#
# Writes are atomic and never widen the mode: the merged JSON goes to a sibling temp file
# created ``0600`` from the first byte, then ``os.replace`` swaps it into place on the same
# filesystem — no world-readable window (#4) and no truncate-on-partial-failure corruption
# (#5). An existing ``settings.json`` that is unparseable, non-object, or has a non-object
# ``env`` is never clobbered: the program fails loudly on STDERR (without the token) and
# exits non-zero so the compose step surfaces the error (#6).
_CLAUDE_MERGE_BODY = """\
import json, os, pwd, sys, tempfile
from pathlib import Path

path = Path.home() / ".claude" / "settings.json"
claude_dir = path.parent
if not claude_dir.is_dir():
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(claude_dir, 0o700)
    except OSError:
        pass  # diagnosed below, naming the owner instead of raising a traceback
try:
    user = pwd.getpwuid(os.getuid()).pw_name
except KeyError:
    user = f"uid {os.getuid()}"
if not claude_dir.is_dir():
    print(
        f"[billet] refusing to write {path}: {claude_dir} does not exist and could not be "
        f"created by {user}; re-run billet start after the Berth 1 entrypoint is in place",
        file=sys.stderr,
    )
    sys.exit(1)
if not os.access(claude_dir, os.W_OK):
    print(
        f"[billet] refusing to write {path}: {claude_dir} is owned by uid "
        f"{claude_dir.stat().st_uid}, not writable by {user}; re-run billet start after "
        "the Berth 1 entrypoint is in place",
        file=sys.stderr,
    )
    sys.exit(1)

data = {}
if path.exists():
    raw = path.read_text()
    try:
        loaded = json.loads(raw)
    except ValueError:
        print(
            f"[billet] refusing to overwrite {path}: file is not valid JSON. "
            "Fix or remove it, then retry.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not isinstance(loaded, dict):
        print(
            f"[billet] refusing to overwrite {path}: top-level JSON is not an object.",
            file=sys.stderr,
        )
        sys.exit(1)
    data = loaded

env = data.get("env", {})
if not isinstance(env, dict):
    print(
        f"[billet] refusing to overwrite {path}: existing 'env' is not an object.",
        file=sys.stderr,
    )
    sys.exit(1)
env["CLAUDE_CODE_OAUTH_TOKEN"] = token
data["env"] = env

fd, tmp = tempfile.mkstemp(dir=str(claude_dir), prefix=".settings.", suffix=".tmp")
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\\n")
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print("[billet] injected CLAUDE_CODE_OAUTH_TOKEN into", path)
"""


def build_claude_merge_program(token: str) -> str:
    """Assemble the full in-container merge program: the ``token`` literal + the body.

    Public (not underscore-private) so the security-critical body can be executed
    end-to-end in a unit test — run under a real ``python3`` with ``HOME`` pointed at a temp
    dir — rather than merely substring-asserted. The token is embedded via ``repr()`` so any
    bytes become a valid, self-escaping Python string literal.
    """
    return f"token = {token!r}\n" + _CLAUDE_MERGE_BODY


# The ADR-0013 repair, applied by the token injection to its own target before the write.
# ``up -d`` returns as soon as PID 1 starts, and on a cold start the Berth entrypoint is
# still generating host keys when this exec runs — so a fresh ``*_claude_home`` volume can
# still be the ``root:root`` directory the daemon created (ADR-0013 cell 1). Same policy as
# the entrypoint: a directory, owned by uid 0, and empty → re-own it to the login user,
# 0700; anything else (populated, third-owner, already ours) is left alone. Never
# recursive. A failed repair warns and continues so the Python program's writability check
# is what names the fault. ``sudo -n`` never prompts (the Berth grants passwordless sudo).
# ``$HOME`` falls back to passwd when the exec environment lacks it — the same fallback
# ``Path.home()`` makes in the program that follows. An unreadable directory fails the
# ``ls -A`` and so counts as not-empty: better an honest error than a blind re-own.
#
# The single ``stat`` yields all three fields the snippet needs — ``%u`` for the uid-0 test,
# ``%U:%G %a`` for the log — so the success line reports the ownership and mode actually
# observed, in the entrypoint's parenthetical shape: ``repaired <path> (was <owner>:<group>
# <mode>)``. One call, so the tested state and the reported state cannot drift apart. The
# ``[billet]`` prefix stays: it names the emitter, and this repair is billet's injection,
# not the entrypoint. A ``stat`` that fails short-circuits the chain, leaving the directory
# alone exactly as the empty-output comparison did before.
CLAUDE_DIR_REPAIR = (
    'd="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}/.claude"; '
    'if [ -d "$d" ] && st="$(stat -c "%u %U:%G %a" "$d")" && [ "${st%% *}" = 0 ] '
    '&& entries="$(ls -A "$d")" && [ -z "$entries" ]; then '
    'if sudo -n install -d -o "$(id -u)" -g "$(id -g)" -m 0700 "$d"; then '
    'echo "[billet] repaired $d (was ${st#* })"; '
    'else echo "[billet] warning: repair of $d failed; continuing" >&2; fi; '
    "fi"
)


def _claude_token_injection(facts: DevcontainerFacts, token: str) -> str:
    """Build the in-container merge step for ``CLAUDE_CODE_OAUTH_TOKEN`` (ADR-0006).

    Runs ``docker compose exec -T -u <remote_user> <service> bash -c '<repair>; exec
    python3 -'`` with the merge program fed on STDIN via a quoted heredoc. The ``bash -c``
    string first applies :data:`CLAUDE_DIR_REPAIR` to ``~/.claude`` (ADR-0013 §6), then
    ``exec``'s ``python3`` with the heredoc still attached as STDIN — one exec session, the
    repair on argv (it carries no secret), the token never. ``-u <remote_user>`` guarantees
    the file is written and owned by the container login user (the same user ``claude``
    runs as over the loopback sshd), so a ``0600`` ``~/.claude/settings.json`` on the
    persisted ``*_claude_home`` volume stays readable, and ``Path.home()`` inside the
    program resolves to that user's home via its uid. The token is embedded as a Python
    ``repr()`` literal inside the heredoc body — so it reaches python3 only as STDIN and
    appears in **no** argv (world-readable via ``ps``/``/proc``) at any hop.
    """
    program = build_claude_merge_program(token)
    exec_cmd = _compose_cmd(
        facts,
        "exec",
        "-T",
        "-u",
        shlex.quote(facts.remote_user),
        shlex.quote(facts.service),
        "bash",
        "-c",
        shlex.quote(f"{CLAUDE_DIR_REPAIR}; exec python3 -"),
    )
    return f"{exec_cmd} <<'{_PYEOF}'\n{program}{_PYEOF}\n"
