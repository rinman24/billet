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
"""

import shlex

from billet.contracts import RemoteHost, WorkspaceSpec
from billet.infrastructure import ssh
from billet.infrastructure.process import ProcessRunner


def _clone_script(spec: WorkspaceSpec) -> str:
    """Build the idempotent remote bash that clones, else fetches + fast-forwards the repo.

    On first use it clones. When the repo is already present it fetches, then converges the
    checked-out branch to its upstream **only** when the move is safe: HEAD is on a branch
    with an upstream, the tracked files are clean, and the change is a genuine fast-forward.
    Untracked files (e.g. a bootstrap-written ``.devcontainer/.env``) are deliberately not
    counted as dirty, so they never block the advance and always survive it. Every skip
    condition prints a one-line ``[billet/source]`` warning and exits 0 — a checkout billet
    cannot safely advance is adopted state, never a reason to fail ``start`` (ADR-0007).
    """
    repo_url = shlex.quote(spec.repo_url)
    repo_dir = shlex.quote(spec.repo_dir)
    return f"""set -euo pipefail
REPO_URL={repo_url}
REPO_DIR={repo_dir}
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[billet/source] cloning $REPO_URL ..."
  git clone "$REPO_URL" "$REPO_DIR"
else
  echo "[billet/source] repo already present; fetching ..."
  cd "$REPO_DIR"
  git fetch --prune
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


class GitSourceAccess:
    """A ``SourceAccess`` that clones then non-destructively fast-forwards over agent-forwarded SSH."""

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner

    def ensure_clone(self, spec: WorkspaceSpec, remote: RemoteHost) -> None:
        """Clone ``repo_url`` into ``repo_dir``, else fetch and safely fast-forward the checkout."""
        argv = ssh.ssh_argv(
            remote.admin_user, remote.ip, _clone_script(spec), tty=True, forward_agent=True
        )
        self._runner.run(argv)
