"""Global ``[billet]`` configuration contract."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GlobalConfig:
    """The ``[billet]`` table: the pinned subscription and an optional default host."""

    subscription_id: str
    default_host: str | None
