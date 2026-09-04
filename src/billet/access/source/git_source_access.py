"""GitSourceAccess — clone then converge a Workspace's repo onto its Host (agent-forwarded).

Mirrors ``remote_clone`` from the lifted ``up.sh``: an agent-forwarded SSH (``ssh -tA``) so
the operator's key flows in over the connection and is **never parked on the Host**. The
step is idempotent — it clones on first use, else fetches and then *non-destructively*
fast-forwards the checked-out branch to its upstream so merged changes on the repo's default
branch (devcontainer.json, Dockerfile, compose) actually take effect on the next ``start``
(ADR-0007). The advance is clean-only and ff-only: it never resets, never discards
uncommitted or untracked files, and leaves any operator- or bootstrap-touched checkout
(dirty tracked files, no upstream, detached HEAD, or a diverged branch) strictly alone with
a ``[billet/source]`` warning.

Every git invocation on the Host runs **non-interactively**: ``start`` is unattended and the
remote's prompts are invisible behind the captured ssh channel, so a git that can prompt
does not fail — it hangs (ADR-0007, "Non-interactive by construction").
"""

import shlex

from billet.contracts import RemoteHost, WorkspaceSpec
from billet.infrastructure import ssh
from billet.infrastructure.process import ProcessRunner
from billet.shared.errors import ProcessError

#: Env exported ahead of every remote git call so a missing credential fails instead of
#: blocking. ``GIT_TERMINAL_PROMPT=0`` kills the ``Username:``/``Password:`` prompt an
#: https ``origin`` triggers; ``BatchMode=yes`` kills ssh's passphrase and confirmation
#: prompts. ``accept-new`` preserves billet's trust-on-first-use posture (the same option
#: :mod:`billet.infrastructure.ssh` uses) so a Host that has never met the forge can still
#: clone, while a *changed* host key still fails.
_GIT_SSH_COMMAND = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"


def _clone_script(spec: WorkspaceSpec) -> str:
    """Build the idempotent remote bash that clones, else fetches + fast-forwards the repo.

    On first use it clones. When the repo is already present it reports any drift between
    the checkout's ``origin`` and the configured ``repo_url``, fetches, then converges the
    checked-out branch to its upstream **only** when the move is safe: HEAD is on a branch
    with an upstream, the tracked files are clean, and the change is a genuine fast-forward.
    Untracked files (e.g. a bootstrap-written ``.devcontainer/.env``) are deliberately not
    counted as dirty, so they never block the advance and always survive it. Every skip
    condition prints a one-line ``[billet/source]`` warning and exits 0 — a checkout billet
    cannot safely advance is adopted state, never a reason to fail ``start`` (ADR-0007).

    Clone and fetch are the two steps that talk to the network, and each is wrapped so its
    failure names the cause (non-interactive git) before aborting.
    """
    repo_url = shlex.quote(spec.repo_url)
    repo_dir = shlex.quote(spec.repo_dir)
    return f"""set -euo pipefail
# Non-interactive by construction: `start` is unattended and the remote's prompts are
# invisible behind the captured ssh channel, so a git that CAN prompt does not fail — it
# hangs. Both variables are exported before any git runs, and cover clone and fetch alike.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND={shlex.quote(_GIT_SSH_COMMAND)}
REPO_URL={repo_url}
REPO_DIR={repo_dir}
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[billet/source] cloning $REPO_URL ..."
  if ! git clone "$REPO_URL" "$REPO_DIR"; then
    echo "[billet/source] clone failed: git ran non-interactively, so it could not ask for a credential or a key passphrase. Check that $REPO_URL is reachable from the Host over the forwarded agent key; an https:// repo_url cannot authenticate here." >&2
    exit 1
  fi
else
  echo "[billet/source] repo already present; fetching ..."
  cd "$REPO_DIR"
  # billet reads the remote and never rewrites it: an origin an operator repointed is
  # adopted state (ADR-0005). Report the drift BEFORE the fetch, so the warning precedes
  # the failure it usually explains.
  origin_url=$(git remote get-url origin 2>/dev/null || true)
  if [ -z "$origin_url" ]; then
    echo "[billet/source] warning: '$REPO_DIR' has no 'origin' remote; billet expects $REPO_URL and will not add it"
  elif [ "$origin_url" != "$REPO_URL" ]; then
    echo "[billet/source] warning: origin fetch URL '$origin_url' differs from the configured repo_url $REPO_URL; billet is not rewriting it"
  fi
  if ! git fetch --prune; then
    echo "[billet/source] fetch failed: git ran non-interactively, so it could not ask for a credential or a key passphrase. If '$REPO_DIR' points at an https:// origin, repoint it at the ssh URL or give the Host a credential helper." >&2
    exit 1
  fi
  # Non-destructively converge the checkout to upstream: advance ONLY on a genuine
  # fast-forward of a clean, tracked branch. Never reset/discard; untracked files (a
  # bootstrap-written .devcontainer/.env) do NOT count as dirty and must survive. Each
  # guard is a condition, so a non-zero probe cannot trip `set -e`; every skip exits 0.
  branch=$(git symbolic-ref --quiet --short HEAD || true)
  if [ -z "$branch" ]; then
    echo "[billet/source] skip fast-forward: HEAD is detached; leaving the checkout untouched"
  elif ! upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{{u}}' 2>/dev/null); then
    echo "[billet/source] skip fast-forward: '$branch' has no upstream; leaving the checkout untouched"
  elif [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "[billet/source] skip fast-forward: tracked files dirty on '$branch'; leaving the checkout untouched"
  elif git merge --ff-only '@{{u}}'; then
    echo "[billet/source] '$branch' is now up to date with '$upstream'"
  else
    echo "[billet/source] skip fast-forward: '$branch' cannot fast-forward to '$upstream' (diverged, or an untracked file blocks it); leaving the checkout untouched"
  fi
fi
"""


def _failure_output(stdout: str, stderr: str) -> str:
    """Join whichever of the captured streams carried the remote's message.

    ``ssh -t`` gives the remote a pty, which merges its stdout and stderr onto one channel —
    so the Host's diagnostics arrive on the *client's stdout*, and the client's own stderr
    carries only ssh-level noise. Reporting ``stderr`` alone would render an empty tail for
    exactly the failures worth reading, so both are kept, in the order they were produced.
    """
    return "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)


class GitSourceAccess:
    """A ``SourceAccess`` that clones then non-destructively fast-forwards over agent-forwarded SSH."""

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner

    def ensure_clone(self, spec: WorkspaceSpec, remote: RemoteHost) -> None:
        """Clone ``repo_url`` into ``repo_dir``, else fetch and safely fast-forward the checkout.

        Raises
        ------
        ProcessError
            If the remote script exits non-zero, carrying the Host's own output so the
            operator reads git's message (and billet's cause line) rather than a bare exit
            code.
        """
        argv = ssh.ssh_argv(
            remote.admin_user, remote.ip, _clone_script(spec), tty=True, forward_agent=True
        )
        result = self._runner.run(argv, check=False)
        if result.returncode != 0:
            raise ProcessError(
                result.argv, result.returncode, _failure_output(result.stdout, result.stderr)
            )
