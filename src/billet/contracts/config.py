"""Global ``[billet]`` configuration contract."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GlobalConfig:
    """The ``[billet]`` table: the pinned subscription and an optional default host.

    ``personal_bootstrap_cmd`` is the operator's personal, in-container bootstrap (e.g.
    installing dotfiles), run in every Workspace's service container right after the
    devcontainer ``postCreateCommand``. Empty (the default) disables it.
    """

    subscription_id: str
    default_host: str | None
    personal_bootstrap_cmd: str
