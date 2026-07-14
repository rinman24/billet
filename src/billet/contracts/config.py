"""Global ``[billet]`` configuration contract."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GlobalConfig:
    """The ``[billet]`` table: the pinned subscription and an optional default host.

    ``personal_bootstrap_cmd`` is the operator's personal, in-container bootstrap (e.g.
    installing dotfiles), run in every Workspace's service container right after the
    devcontainer ``postCreateCommand``. Empty (the default) disables it.

    ``claude_token_cmd`` is the operator-authored shell command billet runs *locally* at
    ``start`` time to fetch the central ``CLAUDE_CODE_OAUTH_TOKEN`` from a credential store
    (Keychain / 1Password / Vault / env). Its STDOUT is captured as the token and injected
    into the Workspace container's user-level ``~/.claude/settings.json`` so ``claude`` there
    authenticates non-interactively (see ADR-0006). Empty (the default) disables it entirely
    — no resolution, no injection.
    """

    subscription_id: str
    default_host: str | None
    personal_bootstrap_cmd: str
    claude_token_cmd: str
