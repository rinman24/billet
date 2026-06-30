"""AzureVmHostProvider — the ``HostProvider`` implemented over the Azure CLI + SSH.

Implements the backend seam structurally (verified where it is injected as a
``HostProvider`` at the composition root). Control-plane ops go through ``az``; host
provisioning (reachability, Docker install) goes over ``ssh``. Mirrors the behavior of the
lifted ``up.sh`` / ``stop.sh`` / ``pin-ip.sh`` host steps.
"""

from collections.abc import Callable
import shlex
import time

from billet.contracts import HostPowerState, HostSpec, HostStatus
from billet.infrastructure import az, ssh
from billet.infrastructure.process import ProcessRunner
from billet.shared.errors import HostOperationError

_POWER_MAP = {
    "VM running": HostPowerState.RUNNING,
    "VM deallocated": HostPowerState.DEALLOCATED,
    "VM stopped": HostPowerState.STOPPED,
}
_DEFAULT_SSH_ATTEMPTS = 30
_SSH_RETRY_SECONDS = 5
_SSH_CONNECT_TIMEOUT = 5


def _docker_install_script(spec: HostSpec) -> str:
    """Build the idempotent remote bash that installs Docker from its signed apt repo."""
    gpg_url = shlex.quote(spec.docker_gpg_url)
    apt_url = shlex.quote(spec.docker_apt_url)
    return f"""set -euo pipefail
if command -v docker >/dev/null 2>&1; then
  echo "[billet/host] docker already present; skipping install"
  exit 0
fi
DOCKER_GPG_URL={gpg_url}
DOCKER_APT_URL={apt_url}
echo "[billet/host] installing Docker from the signed apt repo ..."
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL "$DOCKER_GPG_URL" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
arch="$(dpkg --print-architecture)"
echo "deb [arch=${{arch}} signed-by=/etc/apt/keyrings/docker.gpg] $DOCKER_APT_URL ${{VERSION_CODENAME}} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update -qq
sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
"""


class AzureVmHostProvider:
    """A ``HostProvider`` backed by ``az`` (control plane) and ``ssh`` (provisioning)."""

    def __init__(
        self,
        runner: ProcessRunner,
        *,
        subscription_id: str,
        ssh_attempts: int = _DEFAULT_SSH_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._runner = runner
        self._subscription_id = subscription_id
        self._ssh_attempts = ssh_attempts
        self._sleep = sleep

    def preflight(self) -> None:
        """Require an ``az`` control-plane token and pin the configured subscription."""
        az.require_login(self._runner)
        az.pin_subscription(self._runner, self._subscription_id)

    def status(self, spec: HostSpec) -> HostStatus:
        """Map ``az vm show -d`` power state (+ public IP if running) into a HostStatus."""
        result = self._runner.run(
            [
                "az", "vm", "show", "-d",
                "-g", spec.resource_group, "-n", spec.vm_name,
                "--query", "powerState", "-o", "tsv",
            ],
            check=False,
        )  # fmt: skip
        if result.returncode != 0:
            return HostStatus(power_state=HostPowerState.NOTEXIST, public_ip=None, raw_power="")
        raw = result.stdout.strip()
        power = _POWER_MAP.get(raw, HostPowerState.OTHER)
        public_ip = self._public_ip(spec) if power is HostPowerState.RUNNING else None
        return HostStatus(power_state=power, public_ip=public_ip, raw_power=raw)

    def create(self, spec: HostSpec) -> None:
        """Cold-provision: create the resource group + a tagged VM. BILLABLE."""
        self._runner.run(
            [
                "az", "group", "create",
                "--name", spec.resource_group, "--location", spec.location,
                "--output", "none",
            ]
        )  # fmt: skip
        self._runner.run(
            [
                "az", "vm", "create",
                "--resource-group", spec.resource_group,
                "--name", spec.vm_name,
                "--image", spec.vm_image,
                "--size", spec.vm_size,
                "--os-disk-size-gb", str(spec.os_disk_gb),
                "--storage-sku", spec.storage_sku,
                "--public-ip-sku", spec.public_ip_sku,
                "--admin-username", spec.admin_user,
                "--generate-ssh-keys",
                "--tags", "managed-by=billet", f"billet-host={spec.key}",
                "--output", "none",
            ]
        )  # fmt: skip

    def start(self, spec: HostSpec) -> None:
        """Resume a deallocated VM."""
        self._runner.run(
            ["az", "vm", "start", "--resource-group", spec.resource_group,
             "--name", spec.vm_name, "--output", "none"]
        )  # fmt: skip

    def deallocate(self, spec: HostSpec) -> None:
        """Deallocate the VM (stops compute billing; the OS disk persists)."""
        self._runner.run(
            ["az", "vm", "deallocate", "--resource-group", spec.resource_group,
             "--name", spec.vm_name, "--output", "none"]
        )  # fmt: skip

    def pin_inbound(self, spec: HostSpec) -> str:
        """Re-pin the inbound SSH NSG rule to the operator's current ``/32``.

        The source prefix is always a single ``/32`` — never a wildcard — so SSH is never
        opened to the internet.
        """
        cidr = f"{ssh.operator_egress_ipv4(self._runner)}/32"
        self._runner.run(
            [
                "az", "network", "nsg", "rule", "update",
                "-g", spec.resource_group, "--nsg-name", spec.nsg_name,
                "--name", spec.ssh_rule_name,
                "--source-address-prefixes", cidr,
                "--output", "none",
            ]
        )  # fmt: skip
        return cidr

    def wait_until_reachable(self, spec: HostSpec) -> None:
        """Poll SSH until the host answers, or raise after exhausting attempts."""
        ip = self._require_ip(spec)
        argv = ssh.ssh_argv(
            spec.admin_user, ip, "true",
            connect_timeout=_SSH_CONNECT_TIMEOUT, batch_mode=True,
        )  # fmt: skip
        for attempt in range(1, self._ssh_attempts + 1):
            if self._runner.run(argv, check=False).returncode == 0:
                return
            if attempt < self._ssh_attempts:
                self._sleep(_SSH_RETRY_SECONDS)
        waited = self._ssh_attempts * _SSH_RETRY_SECONDS
        raise HostOperationError(f"SSH did not come up on {ip} after {waited}s")

    def ensure_supply_chain(self, spec: HostSpec) -> None:
        """Idempotently install Docker on the host over SSH."""
        ip = self._require_ip(spec)
        argv = ssh.ssh_argv(spec.admin_user, ip, "bash -se")
        self._runner.run(argv, input_text=_docker_install_script(spec))

    def ensure_tags(self, spec: HostSpec) -> None:
        """Adopt an existing VM by merging billet's ownership tags (idempotent).

        ``az vm update --set tags.<k>=<v>`` merges the named tag keys without disturbing
        any other tags, and is a no-op when they already match — so it is safe to run on
        every ``up`` regardless of who created the VM.
        """
        self._runner.run(
            [
                "az", "vm", "update",
                "-g", spec.resource_group, "-n", spec.vm_name,
                "--set", "tags.managed-by=billet", f"tags.billet-host={spec.key}",
                "--output", "none",
            ]
        )  # fmt: skip

    # --- helpers -------------------------------------------------------------------

    def _public_ip(self, spec: HostSpec) -> str | None:
        result = self._runner.run(
            [
                "az", "vm", "show", "-d",
                "-g", spec.resource_group, "-n", spec.vm_name,
                "--query", "publicIps", "-o", "tsv",
            ],
            check=False,
        )  # fmt: skip
        return result.stdout.strip() or None

    def _require_ip(self, spec: HostSpec) -> str:
        ip = self._public_ip(spec)
        if ip is None:
            raise HostOperationError(
                f"no public IP for {spec.vm_name} — is it running? Run `billet host up` first."
            )
        return ip
