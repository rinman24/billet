"""Tests for the pure Berth drift policy — normalization, directive hash, stamp, diff cap.

The shipped templates are read from the repo here as *fixtures*; the product resolves its
copy from the installed package (tests/unit/test_packaging.py). The vacuity guards pin the
normalizer to real directives so a rotted comment rule fails here instead of making every
file compare equal.
"""

from pathlib import Path

from billet.contracts import BerthFileState, StampState
from billet.workspace.engine.berth_policy import (
    DIFF_CAP,
    assess,
    cap_diff,
    compare_file,
    compare_stamp,
    directive_hash,
    normalize,
)
from tests.unit._fakes import TEMPLATE_DIR, make_berth_read, make_packaged_berth

_REPO_ROOT = Path(__file__).resolve().parents[3]

ENTRYPOINT = (TEMPLATE_DIR / "dev-entrypoint.sh").read_text()
SSHD = (TEMPLATE_DIR / "sshd.conf").read_text()
STUB = (TEMPLATE_DIR / "authorized_keys-stub").read_text()


# --- normalize ----------------------------------------------------------------------


def test_normalize_applies_the_four_rules_in_order() -> None:
    text = "#!/usr/bin/env bash\n\n  # a comment\n   set -eu   \n\necho hi # trailing kept\n"
    assert normalize(text) == ("set -eu", "echo hi # trailing kept")


def test_normalize_folds_backslash_continuations() -> None:
    text = 'echo "a" \\\n     "b" \\\n  "c"\nnext\n'
    assert normalize(text) == ('echo "a" "b" "c"', "next")


def test_a_reindented_continuation_is_a_whitespace_only_change() -> None:
    before = 'echo "dev-entrypoint: skipping ${key}" \\\n     "(value)" >&2\n'
    after = 'echo "dev-entrypoint: skipping ${key}"   \\\n  "(value)" >&2\n'
    assert directive_hash(before) == directive_hash(after)


def test_a_continuation_on_the_last_line_is_kept() -> None:
    assert normalize("tail \\") == ("tail",)


def test_the_stub_normalizes_to_empty_and_compares_ok() -> None:
    assert normalize(STUB) == ()
    other_comments = "# a different header entirely\n\n# still nothing trusted\n"
    assert compare_file("authorized_keys-stub", STUB, other_comments).state is BerthFileState.OK


# --- vacuity guard ----------------------------------------------------------------------


def test_vacuity_guard_normalized_templates_keep_their_directives() -> None:
    # A rotted comment rule (say, dropping every line) would make every copy compare equal;
    # these assertions fail first.
    entry = normalize(ENTRYPOINT)
    sshd = normalize(SSHD)
    assert entry and sshd
    assert len(entry) > 50
    assert "Port 22" in sshd
    assert "PasswordAuthentication no" in sshd
    assert any("sshd" in line for line in entry)
    assert not any(line.startswith("#") for line in (*entry, *sshd))


def test_vacuity_guard_a_directive_change_is_drift() -> None:
    weakened = SSHD.replace("PasswordAuthentication no", "PasswordAuthentication yes")
    assert weakened != SSHD
    status = compare_file("sshd.conf", SSHD, weakened)
    assert status.state is BerthFileState.DRIFT
    assert status.diff == ("-PasswordAuthentication no", "+PasswordAuthentication yes")


# --- compare_file ------------------------------------------------------------------------


def test_billets_own_entrypoint_differs_in_header_only_and_is_ok() -> None:
    own = (_REPO_ROOT / ".devcontainer" / "dev-entrypoint.sh").read_text()
    assert own != ENTRYPOINT  # a real byte difference…
    assert compare_file("dev-entrypoint.sh", ENTRYPOINT, own).state is BerthFileState.OK


def test_a_whitespace_only_change_is_ok() -> None:
    reshaped = "\n\n".join(f"\t  {line}   " for line in SSHD.splitlines())
    assert compare_file("sshd.conf", SSHD, reshaped).state is BerthFileState.OK


def test_a_gswa_shaped_secret_exclude_addition_is_drift_with_the_added_lines() -> None:
    anchor = 'ENV_EXCLUDE="'
    assert anchor in ENTRYPOINT
    gswa = ENTRYPOINT.replace(
        anchor,
        "# Secrets the env republish must never write to /etc/environment.\n"
        'ENV_SECRET_EXCLUDE="AZURE_DEVOPS_EXT_PAT ANTHROPIC_API_KEY"\n' + anchor,
        1,
    )
    gswa += "az devops configure --defaults \\\n    organization=https://dev.azure.com/x\n"
    status = compare_file("dev-entrypoint.sh", ENTRYPOINT, gswa)
    assert status.state is BerthFileState.DRIFT
    assert status.changed_lines == 2
    assert '+ENV_SECRET_EXCLUDE="AZURE_DEVOPS_EXT_PAT ANTHROPIC_API_KEY"' in status.diff
    assert "+az devops configure --defaults organization=https://dev.azure.com/x" in status.diff
    assert not any(line.startswith("+#") for line in status.diff)


def test_a_missing_file_is_missing() -> None:
    assert compare_file("sshd.conf", SSHD, None).state is BerthFileState.MISSING


# --- stamp ---------------------------------------------------------------------------------


def test_stamp_behind_by_n() -> None:
    stamp = compare_stamp(3, "1\n")
    assert stamp.state is StampState.BEHIND
    assert stamp.behind_by == 2


def test_stamp_ahead_means_upgrade_billet() -> None:
    stamp = compare_stamp(1, "2")
    assert stamp.state is StampState.AHEAD
    assert stamp.behind_by == 0


def test_stamp_ok() -> None:
    assert compare_stamp(1, " 1 \n").state is StampState.OK


def test_stamp_missing_or_garbage_is_unknown() -> None:
    assert compare_stamp(1, None).state is StampState.UNKNOWN
    assert compare_stamp(1, "one").state is StampState.UNKNOWN
    assert compare_stamp(1, "0").state is StampState.UNKNOWN


# --- diff cap -------------------------------------------------------------------------------


def test_cap_diff_keeps_the_first_cap_lines_and_counts_the_rest() -> None:
    shown, more = cap_diff([str(n) for n in range(25)])
    assert shown == tuple(str(n) for n in range(DIFF_CAP))
    assert more == 5
    assert cap_diff(["a", "b"]) == (("a", "b"), 0)


def test_more_than_twenty_differing_lines_is_capped() -> None:
    added = "".join(f"echo extra {n}\n" for n in range(30))
    status = compare_file("dev-entrypoint.sh", ENTRYPOINT, ENTRYPOINT + added)
    assert status.changed_lines == 30
    assert status.diff == tuple(f"+echo extra {n}" for n in range(DIFF_CAP))
    assert status.diff_more == 30 - DIFF_CAP


def test_separate_hunks_are_separated_by_a_bare_marker() -> None:
    shipped = "a\nb\nc\nd\ne\n"
    status = compare_file("sshd.conf", shipped, "A\nb\nc\nd\nE\n")
    assert status.diff == ("-a", "+A", "@@", "-e", "+E")
    assert status.changed_lines == 4


# --- assess ---------------------------------------------------------------------------------


def test_assess_builds_one_status_per_workspace() -> None:
    berth = make_packaged_berth()
    read = make_berth_read(overrides={"sshd.conf": None, "berth.version": None})
    status = assess(read, berth, host="devbox", repo_dir="gswa-backend")
    assert status.head == "d223cd5"
    assert status.stamp.state is StampState.UNKNOWN
    assert [(f.file, f.state) for f in status.files] == [
        ("dev-entrypoint.sh", BerthFileState.OK),
        ("sshd.conf", BerthFileState.MISSING),
        ("authorized_keys-stub", BerthFileState.OK),
    ]
