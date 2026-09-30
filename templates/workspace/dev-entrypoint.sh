#!/usr/bin/env bash
# Workspace dev-container entrypoint: bring up the in-container sshd, publish the
# container's environment where sshd login shells can see it, then hand off to the CMD
# (`sleep infinity`). Copy into the repo as .devcontainer/dev-entrypoint.sh and wire it
# as the compose service's entrypoint (see docker-compose.snippet.yml). The venv
# bootstrap belongs in devcontainer.json's postCreateCommand, which billet runs on every
# `billet start` (and VS Code runs on attach) — not here.
#
# sshd must run as root (privilege separation + per-session setuid to `dev`), but the
# container's default user is the non-root `dev` (uid 1000) so login sessions and
# `docker exec` land as dev. So we keep `dev` as default and launch the system sshd via
# the passwordless sudo the image grants.
set -euo pipefail

HOST_KEY_DIR=/etc/ssh/host_keys

# Where pam_env reads the login-shell environment from, and the billet-owned block inside
# it. Overridable only so billet's own test suite can render into a temp file.
ENV_FILE="${BILLET_ENV_FILE:-/etc/environment}"
ENV_BLOCK_BEGIN="# >>> billet dev-entrypoint: container environment (regenerated on start) >>>"
ENV_BLOCK_END="# <<< billet dev-entrypoint <<<"

# Per-process/per-session values a login shell must own for itself, rather than have
# pinned globally to whatever this entrypoint happened to be started with.
ENV_EXCLUDE="HOME PATH SHELL USER LOGNAME PWD OLDPWD HOSTNAME TERM SHLVL _"

# The credential backstop (Berth 2, ADR-0003 amendment 2026-09-30). The snapshot is not a
# secret channel, yet compose `environment:` can still carry a credential into it, so a
# variable whose NAME or VALUE looks like one is withheld and logged by name, never value.
# A consumer that knowingly needs such a variable in ssh sessions lists its exact name in
# BILLET_ENV_PUBLISH (space-separated, set in compose `environment:`). That overrides this
# check only: it cannot publish an ENV_EXCLUDE name or a value pam_env cannot express, and
# BILLET_ENV_PUBLISH itself is never published.
#
# Kit-owned names that match a credential glob but are not credentials. GPG_KEY is the
# python base image's public signing-key id.
ENV_CREDENTIAL_NAME_EXEMPT="GPG_KEY"
# A URL whose userinfo carries a password before the first `/`: scheme://[user]:pass@...
ENV_CREDENTIAL_URL_RE='^[A-Za-z][A-Za-z0-9+.-]*://[^/@:]*:[^/@]*@'

# Succeed when KEY or VALUE looks like a credential. The name globs match
# case-insensitively; `nocasematch`, scoped to the one `case`, does what `${key^^}` would
# while keeping the script runnable by the bash 3.2 that billet's test suite meets on macOS.
env_looks_like_credential() {
    local key="$1" value="$2" by_name=1

    case " ${ENV_CREDENTIAL_NAME_EXEMPT} " in
        *" ${key} "*) ;;
        *)
            shopt -s nocasematch
            case "${key}" in
                *TOKEN* | *SECRET* | *PASSWORD* | *PASSWD* | *_PASS | *PASSPHRASE* | \
                *CREDENTIAL* | *API_KEY* | *ACCESS_KEY* | *PRIVATE_KEY* | *_KEY | *_PAT)
                    by_name=0
                    ;;
            esac
            shopt -u nocasematch
            ;;
    esac
    if [ "${by_name}" -eq 0 ]; then
        return 0
    fi
    [[ ${value} =~ ${ENV_CREDENTIAL_URL_RE} ]]
}

# Render the merged environment file on STDOUT: every line the file already has that
# billet does not own, then a freshly regenerated billet block holding this container's
# environment in pam_env's `KEY="value"` form. Regenerating between the markers keeps the
# write idempotent across restarts while preserving anything the image baked in.
#
# A credential-shaped variable is withheld with a warning unless BILLET_ENV_PUBLISH lists
# it (see env_looks_like_credential above).
#
# pam_env strips ONE pair of surrounding quotes, does no backslash unescaping, and joins a
# line ending in a backslash onto the next — so a value containing `"`, `\` or a control
# character has no faithful representation in this file. Those are skipped with a warning
# rather than written back mangled.
render_container_env() {
    local pair key value
    # Read once; any whitespace separates names, and the padding makes `*" KEY "*` exact.
    local publish=" ${BILLET_ENV_PUBLISH:-} "
    publish="${publish//[[:space:]]/ }"

    if [ -f "${ENV_FILE}" ]; then
        awk -v begin="${ENV_BLOCK_BEGIN}" -v end="${ENV_BLOCK_END}" '
            $0 == begin { drop = 1; next }
            $0 == end   { drop = 0; next }
            !drop
        ' "${ENV_FILE}"
    fi

    printf '%s\n' "${ENV_BLOCK_BEGIN}"
    {
        while IFS= read -r -d '' pair; do
            case "${pair}" in
                *=*) ;;
                *) continue ;;
            esac
            key="${pair%%=*}"
            value="${pair#*=}"
            case " ${ENV_EXCLUDE} " in
                *" ${key} "*) continue ;;
            esac
            [ "${key}" != BILLET_ENV_PUBLISH ] || continue
            case "${key}" in
                "" | [!A-Za-z_]* | *[!A-Za-z0-9_]*) continue ;;
            esac
            case "${publish}" in
                *" ${key} "*) ;;
                *)
                    if env_looks_like_credential "${key}" "${value}"; then
                        echo "dev-entrypoint: withholding ${key} (looks like a credential;" \
                             "list it in BILLET_ENV_PUBLISH to publish)" >&2
                        continue
                    fi
                    ;;
            esac
            case "${value}" in
                *'"'* | *\\* | *[[:cntrl:]]*)
                    echo "dev-entrypoint: skipping ${key}" \
                         "(value cannot be expressed in ${ENV_FILE})" >&2
                    continue
                    ;;
            esac
            printf '%s="%s"\n' "${key}" "${value}"
        done < <(env -0)
    } | LC_ALL=C sort
    printf '%s\n' "${ENV_BLOCK_END}"
}

# --- Berth: mount-target ownership repair (ADR-0013) -----------------------------------
# A named volume mounted at a path the image did not pre-create dev-owned lands
# root:root, and the tool whose state it persists cannot write it. The image is the wrong
# place to guarantee ownership — the entrypoint is the one component present when the
# mount happens, whoever wrote the Dockerfile — so the ownership is repaired here, at the
# one moment both the volume and the login user are present. Policy, per target: a directory owned by uid 0 and EMPTY is
# re-owned to the login user (0700); everything else is reported and left alone, mode
# included. Never recursive, never `chown -R`, never fatal: sshd is the operator's way in
# to fix whatever this could not, so starting it always comes first.

# Print the Docker named-volume mount targets under $HOME, one per line, read from a
# mountinfo(5) file. Field 4 is the mount root — a named volume's is
# `<docker-root>/volumes/<name>/_data` — and field 5 the mountpoint; the kernel escapes
# space, tab, newline and backslash in both as \040, \011, \012 and \134. Bind mounts (the
# workspace checkout, the authorized_keys file) fail the root test and are skipped.
berth_mount_targets() {
    local mountinfo="$1"
    local mount_id parent_id major_minor root mountpoint rest
    [ -r "${mountinfo}" ] || return 0
    while read -r mount_id parent_id major_minor root mountpoint rest; do
        : "${mount_id}" "${parent_id}" "${major_minor}" "${rest}"
        root="${root//\\040/ }"
        root="${root//\\011/$'\t'}"
        root="${root//\\012/$'\n'}"
        root="${root//\\134/\\}"
        mountpoint="${mountpoint//\\040/ }"
        mountpoint="${mountpoint//\\011/$'\t'}"
        mountpoint="${mountpoint//\\012/$'\n'}"
        mountpoint="${mountpoint//\\134/\\}"
        case "${root}" in
            */volumes/*/_data) ;;
            *) continue ;;
        esac
        case "${mountpoint}" in
            "${HOME}"/*) printf '%s\n' "${mountpoint}" ;;
        esac
    done < "${mountinfo}"
}

# Apply the ownership policy to one directory. `berth_ensure_dir <path> create` also makes
# the directory when it is missing (Berth infrastructure such as ~/.ssh); `<path> skip`
# ignores a missing or non-directory path (a mount target that is not a directory is not
# ours to touch).
berth_ensure_dir() {
    local path="$1" missing="$2"
    local uid gid stat_out owner_uid owner_names perms entries
    uid="$(id -u)"
    gid="$(id -g)"

    if [ ! -d "${path}" ]; then
        [ "${missing}" = create ] || return 0
        [ ! -e "${path}" ] || return 0
        if sudo -n install -d -o "${uid}" -g "${gid}" -m 0700 "${path}"; then
            echo "dev-entrypoint: created ${path}"
        else
            echo "dev-entrypoint: warning: repair of ${path} failed; continuing" >&2
        fi
        return 0
    fi

    if ! stat_out="$(stat -c '%u %U:%G %a' "${path}" 2>/dev/null)"; then
        echo "dev-entrypoint: warning: cannot stat ${path}; not repaired" >&2
        return 0
    fi
    read -r owner_uid owner_names perms <<<"${stat_out}"
    [ "${owner_uid}" != "${uid}" ] || return 0
    if [ "${owner_uid}" != 0 ]; then
        echo "dev-entrypoint: warning: ${path} owned by uid ${owner_uid}; not repaired" >&2
        return 0
    fi
    if ! entries="$(find "${path}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)"; then
        echo "dev-entrypoint: warning: cannot read ${path}; not repaired" >&2
        return 0
    fi
    if [ -n "${entries}" ]; then
        echo "dev-entrypoint: warning: ${path} is root-owned and not empty; not repaired" >&2
        return 0
    fi
    if sudo -n install -d -o "${uid}" -g "${gid}" -m 0700 "${path}"; then
        echo "dev-entrypoint: repaired ${path} (was ${owner_names} ${perms})"
    else
        echo "dev-entrypoint: warning: repair of ${path} failed; continuing" >&2
    fi
}

# billet's test suite sources this file with BILLET_ENTRYPOINT_SOURCE_ONLY=1 to exercise
# render_container_env() and the Berth functions without a container. Nothing sets it at
# runtime, so a real entrypoint run always continues past here.
[ -z "${BILLET_ENTRYPOINT_SOURCE_ONLY:-}" ] || return 0

# The Berth revision this script was copied with (ADR-0012). Read from the sibling
# berth.version rather than hardcoded, so a re-copied script cannot misreport it and a
# copy made without the version file says so.
echo "dev-entrypoint: berth=$(cat "$(dirname "$0")/berth.version" 2>/dev/null || echo unknown)"

# Privilege-separation directory sshd requires at runtime (not persisted).
sudo install -d -m 0755 /run/sshd

# Persisted host keys: generated once into the named volume mounted here, then reused
# forever so the container's SSH identity is stable across rebuild/recreate.
sudo install -d -m 0755 "${HOST_KEY_DIR}"

# Berth ownership repair: ~/.ssh first (sshd's authorized_keys bind mount lives under it;
# it is Berth infrastructure, not a Locker), then every named-volume mount target under
# $HOME. Before ssh-keygen — the slow cold-start step — so the window in which a Locker is
# root-owned is as small as this script can make it.
berth_ensure_dir "${HOME}/.ssh" create
while IFS= read -r target; do
    berth_ensure_dir "${target}" skip
done < <(berth_mount_targets /proc/self/mountinfo)

if [ ! -f "${HOST_KEY_DIR}/ssh_host_ed25519_key" ]; then
    echo "dev-entrypoint: generating persisted ed25519 host key"
    sudo ssh-keygen -q -t ed25519 -f "${HOST_KEY_DIR}/ssh_host_ed25519_key" -N ''
fi
if [ ! -f "${HOST_KEY_DIR}/ssh_host_rsa_key" ]; then
    echo "dev-entrypoint: generating persisted rsa host key"
    sudo ssh-keygen -q -t rsa -b 4096 -f "${HOST_KEY_DIR}/ssh_host_rsa_key" -N ''
fi

# Publish the container's environment where sshd login shells will actually see it.
# Docker `ENV` and compose `environment:` reach THIS process, but an sshd session is a
# fresh PAM session that inherits nothing from it — the non-inheritance ADR-0003 records
# and ADR-0006 routes around. Debian's /etc/pam.d/sshd runs `pam_env.so`, which reads
# /etc/environment for every session, so this snapshot is what makes image and compose
# variables visible to `billet connect`, tmux, and anything the fleet launches over ssh.
#
# NOT a secret channel: this file is world-readable and unencrypted, and the skip rule
# above means a value can also be silently dropped. Credentials keep travelling through
# ~/.claude/settings.json (ADR-0006) — never compose `environment:`. The credential check
# in render_container_env() is only a backstop for a credential put there anyway.
echo "dev-entrypoint: publishing the container environment to ${ENV_FILE} for sshd login shells"
ENV_TMP="$(mktemp)"
render_container_env >"${ENV_TMP}"
# Warn rather than abort if the write is refused (a read-only root filesystem, say):
# publishing the environment is secondary to bringing sshd up, and an operator hunting a
# missing variable finds the reason in `docker compose logs` instead of a dead container.
if ! sudo install -m 0644 -o root -g root "${ENV_TMP}" "${ENV_FILE}"; then
    echo "dev-entrypoint: WARNING: could not write ${ENV_FILE};" \
         "image and compose variables will NOT reach sshd login shells" >&2
fi
rm -f "${ENV_TMP}"

# Fail fast with a readable error if the config is bad, rather than a silent no-sshd
# container that looks healthy until you try to connect.
sudo /usr/sbin/sshd -t

# Start sshd as a backgrounded daemon (root via sudo), then exec the CMD. Its
# per-connection children re-parent to PID 1; `sleep infinity` never wait()s, so reaping
# is delegated to docker-init (tini), wired via compose `init: true`.
echo "dev-entrypoint: starting sshd (container :22, published to the VM loopback)"
sudo /usr/sbin/sshd

exec "$@"
