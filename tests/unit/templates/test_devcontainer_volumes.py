"""Tests for the persisted-state contract the Workspace templates prescribe.

A Workspace container is rebuilt often (``compose up --build``), so anything the operator
authenticated by hand has to live on a named volume — a **Locker** — or it is lost every
rebuild. The compose snippet declares and mounts the volume; ownership of the mount target
is the Berth entrypoint's job at container start (ADR-0013, ``test_berth.py``), so the
Dockerfile pre-creates no Locker mountpoint. Until Berth 1 that guarantee lived in the
image (``install -d -o dev`` before ``USER dev``) and was asserted here; those assertions
retired with it.

Authentication tooling is where the persistence pairing lives. Issue #66 took ``gh`` out
of the base templates: not every Workspace calls ``gh`` or ``az``, so each CLI became an
opt-in recipe under ``templates/workspace/auth-tooling/`` — and a recipe is itself two
parts, the CLI baked into the image and its credential directory on a volume. Adopting one
part only is the failure the recipe exists to prevent: it looks like it works until the
next rebuild, which either reinstalls the binary by hand or asks for ``gh auth login``
again.

So these tests hold three seams together. Inside a recipe: mount, declaration, the apt
install that makes persisting a credential store worth anything, and the GPG pin on the
third-party source it comes from. Across the base templates: the absence of all of it,
which is the point of #66 — a Workspace that needs neither CLI must carry neither. And in
billet's own ``.devcontainer/``: billet runs itself as a Workspace and needs both CLIs
(``az`` manages Hosts, ``gh`` drives pull requests), so it implements both recipes and is
consumer #1 of its own contract. A template change that is not mirrored there has not been
dogfooded.
"""

from dataclasses import dataclass
from pathlib import Path
import re

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

_TEMPLATE_DIR = _REPO_ROOT / "templates" / "workspace"
_TEMPLATE_COMPOSE = _TEMPLATE_DIR / "docker-compose.snippet.yml"
_TEMPLATE_DOCKERFILE = _TEMPLATE_DIR / "Dockerfile.snippet"
_AUTH_TOOLING_DIR = _TEMPLATE_DIR / "auth-tooling"
_DEVCONTAINER_COMPOSE = _REPO_ROOT / ".devcontainer" / "docker-compose.yml"
_DEVCONTAINER_DOCKERFILE = _REPO_ROOT / ".devcontainer" / "Dockerfile"

#: The compose service placeholder an adopting repo replaces; billet's own value is
#: ``billet``, which is what makes the template's volume names comparable to billet's.
_SERVICE_PLACEHOLDER = "<service>"
_BILLET_SERVICE = "billet"

#: Where a third-party apt source's signing key belongs, and the pin that ties a source
#: list to it. billet's own Dockerfile already does this for nodesource and Microsoft; the
#: recipes reuse the convention rather than trusting an unpinned repository.
_KEYRING_DIR = "/etc/apt/keyrings"

#: A compose mount whose source names a volume rather than a path. Docker treats a source
#: containing ``/`` or starting with ``.`` as a bind mount; ``${VAR:-default}``
#: interpolations are bind mounts here too. Angle brackets admit the ``<service>``
#: placeholder the templates carry.
_NAMED_VOLUME_MOUNT = re.compile(
    r"^-\s+(?P<source>[A-Za-z0-9_<][A-Za-z0-9_.<>-]*):(?P<target>/[^:]+)(?::[a-z,]+)?$"
)

#: A top-level volume declaration, e.g. ``  billet_gh_config:``.
_VOLUME_DECLARATION = re.compile(r"^\s+(?P<name>[A-Za-z0-9_<][A-Za-z0-9_.<>-]*):\s*$")

#: A compose ``environment:`` entry in mapping form, ``NAME: value``.
_ENVIRONMENT_ENTRY = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*):\s+(?P<value>\S.*?)\s*$")

#: The Claude Locker: the one Locker in the base snippet (ADR-0006's target) and the
#: variable that pins Claude Code to it. The target is fixed by billet's injector, which
#: writes ``~/.claude/settings.json`` for the ``dev`` user.
_CLAUDE_LOCKER_SUFFIX = "_claude_home"
_CLAUDE_LOCKER_TARGET = "/home/dev/.claude"
_CLAUDE_CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"

#: An ``install -d`` that creates a 0700 directory owned by the non-root ``dev`` user.
_DEV_OWNED_INSTALL = re.compile(
    r"install\s+-d\s+-o\s+dev\s+-g\s+dev\s+-m\s+0700\s+(?P<path>/[^\s;\\]+)"
)

#: An ``apt-get install`` and everything it is handed. Applied to one command at a time,
#: so the match runs to the end of that command's argument list.
_APT_INSTALL = re.compile(r"\bapt(?:-get)?\s+install\b(?P<arguments>.*)")

#: A keyring written into place (``curl -o …`` or ``gpg --dearmor -o …``), and a source
#: list pinned to one.
_KEYRING_WRITE = re.compile(rf"-o\s+(?P<keyring>{_KEYRING_DIR}/[A-Za-z0-9._-]+)")
_SIGNED_BY = re.compile(rf"signed-by=(?P<keyring>{_KEYRING_DIR}/[A-Za-z0-9._-]+)")


@dataclass(frozen=True)
class _Recipe:
    """One opt-in auth-tooling recipe: a CLI, its credential volume, and both snippets.

    Attributes
    ----------
    name
        The recipe's file prefix under ``auth-tooling/``, which is also the CLI's command
        name and the id this recipe is parameterized under.
    package
        The Debian package the recipe's Dockerfile snippet must apt-install.
    volume_suffix
        What the recipe appends to the compose service name to form the volume name.
    mountpoint
        The container path the credential volume lands on.
    """

    name: str
    package: str
    volume_suffix: str
    mountpoint: str

    @property
    def dockerfile(self) -> Path:
        """The recipe's Dockerfile snippet — the CLI half."""
        return _AUTH_TOOLING_DIR / f"{self.name}.Dockerfile.snippet"

    @property
    def compose(self) -> Path:
        """The recipe's compose snippet — the credential-volume half."""
        return _AUTH_TOOLING_DIR / f"{self.name}.docker-compose.snippet.yml"

    def volume(self, service: str) -> str:
        """The credential volume's name for a given compose service.

        Parameters
        ----------
        service
            The compose service name: ``<service>`` while it is still a template
            placeholder, ``billet`` in billet's own devcontainer.

        Returns
        -------
        str
            The named volume this recipe mounts and declares.
        """
        return f"{service}{self.volume_suffix}"


#: The recipes issue #66 split out of the base templates. ``gh`` keeps ``hosts.yml`` under
#: ~/.config/gh — the credential store issue #58 was about; ``az`` writes its Entra refresh
#: token straight into ~/.azure.
_RECIPES = [
    _Recipe(
        name="gh",
        package="gh",
        volume_suffix="_gh_config",
        mountpoint="/home/dev/.config/gh",
    ),
    _Recipe(
        name="az",
        package="azure-cli",
        volume_suffix="_azure_home",
        mountpoint="/home/dev/.azure",
    ),
]
_RECIPE_IDS = [recipe.name for recipe in _RECIPES]

#: Every compose file that mounts named volumes: the base template, billet's own, and each
#: recipe's fragment.
_COMPOSE_FILES = [_TEMPLATE_COMPOSE, _DEVCONTAINER_COMPOSE, *(r.compose for r in _RECIPES)]
_COMPOSE_IDS = ["template", "devcontainer", *(f"{r.name}-recipe" for r in _RECIPES)]


def _named_volume_mounts(compose: Path) -> dict[str, str]:
    """Map each named volume a compose file mounts to the container path it lands on.

    Only service-level ``volumes:`` blocks (which are indented) are read; the top-level
    ``volumes:`` mapping declares names and mounts nothing.

    Parameters
    ----------
    compose
        The compose file to scan.

    Returns
    -------
    dict[str, str]
        Volume name to container path, for named volumes only. Bind mounts are skipped.
    """
    mounts: dict[str, str] = {}
    block_indent: int | None = None
    for raw in compose.read_text().splitlines():
        stripped: str = raw.strip()
        indent: int = len(raw) - len(raw.lstrip())
        if not stripped or stripped.startswith("#"):
            continue
        if block_indent is not None and indent <= block_indent:
            block_indent = None
        if stripped == "volumes:" and indent > 0:
            block_indent = indent
            continue
        if block_indent is None:
            continue
        match = _NAMED_VOLUME_MOUNT.match(stripped)
        if match is not None:
            mounts[match.group("source")] = match.group("target")
    return mounts


def _environment_mapping(compose: Path) -> dict[str, str]:
    """Map each ``environment:`` entry a compose file sets in mapping form to its value.

    Only service-level ``environment:`` blocks (which are indented) are read, and only the
    ``NAME: value`` mapping form — the list form (``- NAME=value``) is the naming scan's
    business in ``test_env_var_naming``, since a list entry there reads as an assignment.

    Parameters
    ----------
    compose
        The compose file to scan.

    Returns
    -------
    dict[str, str]
        Variable name to the literal value the file sets.
    """
    entries: dict[str, str] = {}
    block_indent: int | None = None
    for raw in compose.read_text().splitlines():
        stripped: str = raw.strip()
        indent: int = len(raw) - len(raw.lstrip())
        if not stripped or stripped.startswith("#"):
            continue
        if block_indent is not None and indent <= block_indent:
            block_indent = None
        if stripped == "environment:" and indent > 0:
            block_indent = indent
            continue
        if block_indent is None:
            continue
        match = _ENVIRONMENT_ENTRY.match(stripped)
        if match is not None:
            entries[match.group("name")] = match.group("value")
    return entries


def _declared_volumes(compose: Path) -> set[str]:
    """The names in a compose file's top-level ``volumes:`` mapping.

    Parameters
    ----------
    compose
        The compose file to scan.

    Returns
    -------
    set[str]
        Every declared volume name.
    """
    declared: set[str] = set()
    in_block: bool = False
    for raw in compose.read_text().splitlines():
        stripped: str = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw == "volumes:":
            in_block = True
            continue
        if not in_block:
            continue
        if not raw.startswith((" ", "\t")):
            break
        match = _VOLUME_DECLARATION.match(raw)
        if match is not None:
            declared.add(match.group("name"))
    return declared


def _directive_text(dockerfile: Path) -> str:
    """A Dockerfile's build directives: comment lines dropped, continuations folded in.

    Comments go first so the prose explaining a recipe can never satisfy an assertion
    about what that recipe builds — every snippet here describes the CLI it installs and
    the mountpoint it creates in its own header. Continued lines are then folded into the
    command they belong to, so a package list spread over a dozen lines reads as one.

    Parameters
    ----------
    dockerfile
        The Dockerfile (or merge snippet) to read.

    Returns
    -------
    str
        The remaining text, one logical command per line.
    """
    lines: list[str] = [
        line for line in dockerfile.read_text().splitlines() if not line.lstrip().startswith("#")
    ]
    return "\n".join(lines).replace("\\\n", " ")


def _dev_owned_precreations(dockerfile: Path) -> dict[str, int]:
    """Map each 0700 dev-owned directory a Dockerfile creates to where it first appears.

    Offsets index :func:`_directive_text`'s output, which is what makes them comparable
    with the position of the file's other directives — ``USER dev`` in particular.

    Parameters
    ----------
    dockerfile
        The Dockerfile (or merge snippet) to scan.

    Returns
    -------
    dict[str, int]
        Absolute container path to the offset of the ``install -d`` that creates it.
    """
    creations: dict[str, int] = {}
    for match in _DEV_OWNED_INSTALL.finditer(_directive_text(dockerfile)):
        creations.setdefault(match.group("path"), match.start())
    return creations


def _precreates_dev_owned(dockerfile: Path, path: str) -> bool:
    """Whether a Dockerfile creates ``path`` as a 0700 directory owned by ``dev``.

    Parameters
    ----------
    dockerfile
        The Dockerfile (or merge snippet) to scan.
    path
        The absolute container path that must be pre-created.

    Returns
    -------
    bool
        ``True`` when an ``install -d`` for exactly that path carries dev ownership and
        mode 0700. A parent created on the way does not count — it gets neither.
    """
    return path in _dev_owned_precreations(dockerfile)


def _apt_installed_packages(dockerfile: Path) -> set[str]:
    """Every package name an ``apt-get install`` in a Dockerfile hands to apt.

    Directives come from :func:`_directive_text` and are then split on ``;`` and ``&&``,
    so a multi-command ``RUN`` is read one command at a time. Flags are ignored, as is the
    suite argument of ``-t`` (billet pulls git from bookworm-backports that way).

    Parameters
    ----------
    dockerfile
        The Dockerfile (or merge snippet) to scan.

    Returns
    -------
    set[str]
        The packages installed, across every apt invocation in the file.
    """
    packages: set[str] = set()
    for line in _directive_text(dockerfile).splitlines():
        for command in re.split(r";|&&", line):
            invocation: re.Match[str] | None = _APT_INSTALL.search(command)
            if invocation is None:
                continue
            skip_value: bool = False
            for token in invocation.group("arguments").split():
                if skip_value:
                    skip_value = False
                elif token in {"-t", "--target-release"}:
                    skip_value = True
                elif not token.startswith("-"):
                    packages.add(token)
    return packages


@pytest.mark.parametrize("recipe", _RECIPES, ids=_RECIPE_IDS)
def test_a_recipe_names_its_locker_canonically(recipe: _Recipe) -> None:
    # The volume part of a recipe is one compose Locker, under the canonical name billet's
    # docs use (`<service>_gh_config`, `<service>_azure_home`), mounted at the tool's config
    # directory and declared — a mount with no declaration fails at `up`, i.e. only on the
    # VM, so the snippet an adopting repo copies must be a whole volume. Ownership of the
    # target is no longer the recipe's business: the Berth entrypoint repairs it at mount
    # time (ADR-0013), which is what retired the third part of the old three-part recipe.
    volume: str = recipe.volume(_SERVICE_PLACEHOLDER)
    assert _named_volume_mounts(recipe.compose).get(volume) == recipe.mountpoint, (
        f"{recipe.compose.relative_to(_REPO_ROOT)} must mount the named volume `{volume}` "
        f"at {recipe.mountpoint} so `{recipe.name}` credentials survive a rebuild."
    )
    assert volume in _declared_volumes(recipe.compose), (
        f"{recipe.compose.relative_to(_REPO_ROOT)} mounts `{volume}` without declaring it "
        "under the top-level `volumes:` mapping, so the snippet an adopting repo copies "
        "is half a volume."
    )


@pytest.mark.parametrize("recipe", _RECIPES, ids=_RECIPE_IDS)
def test_a_recipe_dockerfile_creates_no_mountpoint(recipe: _Recipe) -> None:
    # ADR-0013: a recipe is two parts, binary and volume. The image-side `install -d` that
    # used to be its third part is gone — the Berth entrypoint re-owns a root-owned, empty
    # Locker at container start, so a snippet that still pre-creates one tells the next
    # reader the repair is not to be trusted.
    created: dict[str, int] = _dev_owned_precreations(recipe.dockerfile)
    assert not created, (
        f"{recipe.dockerfile.relative_to(_REPO_ROOT)} still pre-creates {sorted(created)}; "
        "a recipe's Dockerfile snippet installs the binary and nothing else — Locker "
        "ownership is the entrypoint's job at mount time (ADR-0013)."
    )


@pytest.mark.parametrize("recipe", _RECIPES, ids=_RECIPE_IDS)
def test_a_recipe_supplies_both_the_cli_and_its_credential_volume(recipe: _Recipe) -> None:
    # Adopting one half only is the failure mode the recipe exists to prevent: with the
    # CLI but no volume, every `compose up --build` forces `gh auth login` / `az login`
    # again; with the volume but no CLI, the binary is reinstalled by hand into
    # ~/.local/bin, which no volume covers, so the rebuild eats that instead. Either way
    # it looks like it works right until the next rebuild, so a recipe ships both.
    assert recipe.package in _apt_installed_packages(recipe.dockerfile), (
        f"{recipe.dockerfile.relative_to(_REPO_ROOT)} must apt-install `{recipe.package}`; "
        "a recipe that only persists a credential directory leaves the CLI itself to be "
        "reinstalled by hand after every rebuild."
    )
    assert recipe.mountpoint in _named_volume_mounts(recipe.compose).values(), (
        f"{recipe.compose.relative_to(_REPO_ROOT)} must persist {recipe.mountpoint} on a "
        f"named volume; a recipe that only bakes `{recipe.package}` into the image still "
        "costs an interactive login after every rebuild."
    )


@pytest.mark.parametrize("recipe", _RECIPES, ids=_RECIPE_IDS)
def test_a_recipe_pins_its_apt_source_to_a_keyring(recipe: _Recipe) -> None:
    # An unpinned third-party source trusts every key in the system keyring, so the CLI
    # billet's own workflow authenticates with would come from whatever that repository
    # serves. billet's Dockerfile already pins nodesource and Microsoft this way; a recipe
    # copied into someone else's image has to carry the same shape with it.
    text: str = _directive_text(recipe.dockerfile)
    written: set[str] = {match.group("keyring") for match in _KEYRING_WRITE.finditer(text)}
    pinned: set[str] = {match.group("keyring") for match in _SIGNED_BY.finditer(text)}
    assert written, (
        f"{recipe.dockerfile.relative_to(_REPO_ROOT)} must fetch the vendor's signing key "
        f"into {_KEYRING_DIR}/ rather than adding it to the system keyring."
    )
    assert pinned, (
        f"{recipe.dockerfile.relative_to(_REPO_ROOT)} must pin its source list with "
        f"`signed-by={_KEYRING_DIR}/…`; without it apt accepts the repository's packages "
        "under any trusted key."
    )
    assert pinned <= written, (
        f"{recipe.dockerfile.relative_to(_REPO_ROOT)} pins {sorted(pinned - written)}, "
        f"which the snippet never writes — apt would find no such keyring and refuse the "
        "source. Fetch the key to the same path the source list names."
    )


@pytest.mark.parametrize("recipe", _RECIPES, ids=_RECIPE_IDS)
def test_the_base_templates_carry_no_cli_specific_auth_tooling(recipe: _Recipe) -> None:
    # The regression (issue #66): `gh` lived in the base templates, so every Workspace
    # adopting them got a CLI it might never call and a volume it never filled. Auth
    # tooling is opt-in now — a Workspace that needs neither CLI must carry neither, and
    # the base snippets are what a repo adopts before choosing any recipe.
    assert not _precreates_dev_owned(_TEMPLATE_DOCKERFILE, recipe.mountpoint), (
        f"{_TEMPLATE_DOCKERFILE.relative_to(_REPO_ROOT)} pre-creates {recipe.mountpoint}, "
        f"which belongs to the opt-in `{recipe.name}` recipe. The base snippet creates only "
        "what every Workspace needs; move it back to auth-tooling/."
    )
    assert recipe.package not in _apt_installed_packages(_TEMPLATE_DOCKERFILE), (
        f"{_TEMPLATE_DOCKERFILE.relative_to(_REPO_ROOT)} installs `{recipe.package}`, which "
        f"belongs to the opt-in `{recipe.name}` recipe. Not every Workspace calls it, so "
        "the base image must not carry it."
    )
    names: set[str] = set(_named_volume_mounts(_TEMPLATE_COMPOSE)) | _declared_volumes(
        _TEMPLATE_COMPOSE
    )
    offenders: set[str] = {name for name in names if name.endswith(recipe.volume_suffix)}
    assert not offenders, (
        f"{_TEMPLATE_COMPOSE.relative_to(_REPO_ROOT)} still carries {sorted(offenders)}, "
        f"the `{recipe.name}` recipe's credential volume. The base compose snippet declares "
        "only the volumes every Workspace uses."
    )


@pytest.mark.parametrize(
    ("compose", "service"),
    [(_TEMPLATE_COMPOSE, _SERVICE_PLACEHOLDER), (_DEVCONTAINER_COMPOSE, _BILLET_SERVICE)],
    ids=["template", "devcontainer"],
)
def test_the_base_compose_carries_the_claude_locker(compose: Path, service: str) -> None:
    # The Claude Locker is the one Locker in the base snippet, not a recipe: every billet
    # Workspace receives a token (ADR-0006), and the token lands in ~/.claude/settings.json.
    # CLAUDE_CONFIG_DIR pins `claude` to that same directory; its value is fixed because
    # the injector hardcodes the path, so any other value would split the two.
    volume: str = f"{service}{_CLAUDE_LOCKER_SUFFIX}"
    assert _named_volume_mounts(compose).get(volume) == _CLAUDE_LOCKER_TARGET, (
        f"{compose.relative_to(_REPO_ROOT)} must mount `{volume}` at {_CLAUDE_LOCKER_TARGET} "
        "— the Claude Locker the ADR-0006 injection writes into."
    )
    assert volume in _declared_volumes(compose), (
        f"{compose.relative_to(_REPO_ROOT)} mounts `{volume}` without declaring it under the "
        "top-level `volumes:` mapping."
    )
    assert _environment_mapping(compose).get(_CLAUDE_CONFIG_DIR_VAR) == _CLAUDE_LOCKER_TARGET, (
        f"{compose.relative_to(_REPO_ROOT)} must set `{_CLAUDE_CONFIG_DIR_VAR}: "
        f"{_CLAUDE_LOCKER_TARGET}` under the service's `environment:` (mapping form) so "
        "`claude` reads the same directory billet's injection writes."
    )


@pytest.mark.parametrize("compose", _COMPOSE_FILES, ids=_COMPOSE_IDS)
def test_every_mounted_named_volume_is_declared(compose: Path) -> None:
    # Compose errors out at `up` on an undeclared named volume, i.e. only on the VM, long
    # after the review that introduced it. Recipe snippets are checked too: they are
    # copied as a unit, so a declaration missing there is missing in every repo that
    # adopts them.
    mounted: set[str] = set(_named_volume_mounts(compose))
    assert mounted, (
        f"{compose.relative_to(_REPO_ROOT)} mounts no named volumes at all. Either it "
        "stopped persisting state or this scan's regexes no longer match it — a contract "
        "test that reads nothing passes vacuously forever."
    )
    undeclared: set[str] = mounted - _declared_volumes(compose)
    assert not undeclared, (
        f"{compose.relative_to(_REPO_ROOT)} mounts {sorted(undeclared)} without declaring "
        "them under the top-level `volumes:` mapping."
    )


@pytest.mark.parametrize(
    "dockerfile",
    [_TEMPLATE_DOCKERFILE, _DEVCONTAINER_DOCKERFILE],
    ids=["template", "devcontainer"],
)
def test_ssh_is_the_only_pre_created_locker_shaped_directory(dockerfile: Path) -> None:
    # Berth 1 (ADR-0013): the image guarantees ~/.ssh — Berth infrastructure, 0700 so the
    # authorized_keys bind mount is StrictModes-clean — and pre-creates no Locker
    # mountpoint, because a pre-created one in the reference implementation tells the next
    # reader it is still required. The entrypoint repairs ownership at mount time instead.
    # ~/.config is tolerated in billet's own image: it is not a mount target, and chezmoi
    # writes into it before any Locker is involved.
    creations: dict[str, int] = _dev_owned_precreations(dockerfile)
    assert "/home/dev/.ssh" in creations, (
        f"{dockerfile.relative_to(_REPO_ROOT)} must `install -d -o dev -g dev -m 0700 "
        "/home/dev/.ssh`; sshd's authorized_keys bind mount lands under it."
    )
    lockers: set[str] = {recipe.mountpoint for recipe in _RECIPES} | {"/home/dev/.claude"}
    pre_created: set[str] = set(creations) & lockers
    assert not pre_created, (
        f"{dockerfile.relative_to(_REPO_ROOT)} still pre-creates {sorted(pre_created)}. Since "
        "Berth 1 the entrypoint repairs a Locker's ownership at mount time; drop the line so "
        "the reference implementation exercises the repair."
    )


@pytest.mark.parametrize("recipe", _RECIPES, ids=_RECIPE_IDS)
def test_billet_devcontainer_implements_the_recipe_it_depends_on(recipe: _Recipe) -> None:
    # billet needs both CLIs — `az` because managing Azure Hosts is its job, `gh` because
    # its own workflow is pull requests — so it is consumer #1 of both recipes. A recipe
    # billet does not run itself has never actually been exercised, and half of it here is
    # the same slow failure it warns adopters about: the next rebuild asks for a login.
    volume: str = recipe.volume(_BILLET_SERVICE)
    assert _named_volume_mounts(_DEVCONTAINER_COMPOSE).get(volume) == recipe.mountpoint, (
        f"{_DEVCONTAINER_COMPOSE.relative_to(_REPO_ROOT)} must mount `{volume}` at "
        f"{recipe.mountpoint}, the `{recipe.name}` recipe with `{_SERVICE_PLACEHOLDER}` = "
        f"`{_BILLET_SERVICE}`."
    )
    assert volume in _declared_volumes(_DEVCONTAINER_COMPOSE), (
        f"{_DEVCONTAINER_COMPOSE.relative_to(_REPO_ROOT)} mounts `{volume}` without "
        "declaring it under the top-level `volumes:` mapping."
    )
    assert recipe.package in _apt_installed_packages(_DEVCONTAINER_DOCKERFILE), (
        f"{_DEVCONTAINER_DOCKERFILE.relative_to(_REPO_ROOT)} must apt-install "
        f"`{recipe.package}`: billet persists {recipe.mountpoint} on a volume, so it has "
        f"adopted half the `{recipe.name}` recipe — the half that stores a credential for a "
        "CLI the image does not ship."
    )


def test_billet_devcontainer_implements_every_volume_the_template_prescribes() -> None:
    # billet runs itself as a Workspace, so it is consumer #1 of these templates: a
    # template volume that is not mirrored here has never actually been exercised. billet's
    # compose is a superset — it adds both recipes' volumes plus its own Claude home.
    template: dict[str, str] = {
        name.replace(_SERVICE_PLACEHOLDER, _BILLET_SERVICE): target
        for name, target in _named_volume_mounts(_TEMPLATE_COMPOSE).items()
    }
    devcontainer: dict[str, str] = _named_volume_mounts(_DEVCONTAINER_COMPOSE)
    missing: dict[str, str] = {
        name: target for name, target in template.items() if devcontainer.get(name) != target
    }
    assert not missing, (
        "billet's own .devcontainer/docker-compose.yml is missing the template's named "
        f"volumes {sorted(missing)} (with `{_SERVICE_PLACEHOLDER}` = `{_BILLET_SERVICE}`). "
        "Port the template change into billet's devcontainer as well."
    )
