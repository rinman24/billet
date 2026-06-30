"""Azure CLI primitives: auth preflight and deterministic subscription pinning.

These wrap a :class:`ProcessRunner` and return primitives only — they never construct
domain contracts (that is the access layer's job). Mirrors ``require_az_login`` /
``pin_subscription`` from the lifted devbox bash.
"""

from billet.infrastructure.process import ProcessRunner
from billet.shared.errors import AzLoginRequired, HostOperationError

_MGMT_SCOPE = "https://management.azure.com/.default"


def require_login(runner: ProcessRunner) -> None:
    """Raise :class:`AzLoginRequired` unless a control-plane token is available.

    We never drive an interactive ``az login`` ourselves — we gate and instruct.
    """
    result = runner.run(
        [
            "az",
            "account",
            "get-access-token",
            "--scope",
            _MGMT_SCOPE,
            "--query",
            "expiresOn",
            "-o",
            "tsv",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise AzLoginRequired(
            "Azure CLI is not logged in (control-plane token unavailable). "
            f"Run: az login --scope {_MGMT_SCOPE}"
        )


def pin_subscription(runner: ProcessRunner, subscription_id: str) -> None:
    """Pin the active subscription deterministically and verify it took.

    ``az account set`` only mutates local CLI config (not a billable resource), so it is
    safe to run even under dry-run, where downstream read-only queries must target the
    right subscription to render an accurate plan.
    """
    runner.run(["az", "account", "set", "--subscription", subscription_id])
    active = runner.run(["az", "account", "show", "--query", "id", "-o", "tsv"]).stdout.strip()
    if active != subscription_id:
        raise HostOperationError(
            f"failed to pin subscription to {subscription_id} (active is {active})"
        )
