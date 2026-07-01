"""GitSourceAccess — clone/fetch a Workspace's repo onto its Host (agent-forwarded).

Mirrors ``remote_clone`` from the lifted ``up.sh``: an agent-forwarded SSH (``ssh -tA``) so
the operator's key flows in over the connection and is **never parked on the Host**. The
clone is idempotent — it fetches when the repo is already present.
"""

import shlex

from billet.contracts import RemoteHost, WorkspaceSpec
from billet.infrastructure import ssh
from billet.infrastructure.process import ProcessRunner


def _clone_script(spec: WorkspaceSpec) -> str:
    """Build the idempotent remote bash that clones-or-fetches the repo."""
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
  git -C "$REPO_DIR" fetch --prune
fi
"""


class GitSourceAccess:
    """A ``SourceAccess`` that clones/fetches over an agent-forwarded SSH to the Host."""

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner

    def ensure_clone(self, spec: WorkspaceSpec, remote: RemoteHost) -> None:
        """Clone ``repo_url`` into ``repo_dir`` on the host, or fetch if already cloned."""
        argv = ssh.ssh_argv(
            remote.admin_user, remote.ip, _clone_script(spec), tty=True, forward_agent=True
        )
        self._runner.run(argv)
