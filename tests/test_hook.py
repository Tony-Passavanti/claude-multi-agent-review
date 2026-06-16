"""Tests for src/hook.py.

Covers the pure parsers (is_zero_sha, parse_stdin), the payload formatter
(format_diff_payload), and the derived properties on RefUpdate /
ReviewPayload. The git-touching helpers (build_payload, _git, etc.) are
exercised in the integration tests, except where a regression-prone
detail (e.g. rename semantics on the gate-feeding path) needs targeted
coverage and is included here.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from src.hook import (
    EMPTY_TREE_SHA,
    RefReview,
    RefUpdate,
    ReviewPayload,
    _diff_changed_files,
    format_diff_payload,
    is_zero_sha,
    parse_stdin,
)


# --- is_zero_sha -----------------------------------------------------------

def test_is_zero_sha_sha1() -> None:
    assert is_zero_sha("0" * 40) is True


def test_is_zero_sha_sha256() -> None:
    assert is_zero_sha("0" * 64) is True


def test_is_zero_sha_single_zero() -> None:
    # Length-agnostic by design.
    assert is_zero_sha("0") is True


def test_is_zero_sha_non_zero() -> None:
    assert is_zero_sha("abc123") is False
    assert is_zero_sha("0" * 39 + "1") is False
    assert is_zero_sha("1" + "0" * 39) is False


def test_is_zero_sha_empty_string() -> None:
    assert is_zero_sha("") is False


def test_is_zero_sha_known_empty_tree_sha_is_not_zero() -> None:
    # Defensive: the well-known empty-tree SHA must not be treated as
    # the all-zeros placeholder, even though it represents "nothing."
    assert is_zero_sha(EMPTY_TREE_SHA) is False


# --- parse_stdin -----------------------------------------------------------

def _stdin(text: str) -> io.StringIO:
    return io.StringIO(text)


def test_parse_stdin_empty_returns_empty_list() -> None:
    assert parse_stdin(_stdin("")) == []


def test_parse_stdin_only_blank_lines() -> None:
    assert parse_stdin(_stdin("\n\n  \n")) == []


def test_parse_stdin_single_line() -> None:
    line = "refs/heads/foo abc123 refs/heads/foo def456\n"
    refs = parse_stdin(_stdin(line))
    assert len(refs) == 1
    assert refs[0] == RefUpdate(
        local_ref="refs/heads/foo",
        local_sha="abc123",
        remote_ref="refs/heads/foo",
        remote_sha="def456",
    )


def test_parse_stdin_multiple_lines() -> None:
    lines = (
        "refs/heads/foo a1 refs/heads/foo a2\n"
        "refs/heads/bar b1 refs/heads/bar b2\n"
    )
    refs = parse_stdin(_stdin(lines))
    assert len(refs) == 2
    assert refs[0].local_ref == "refs/heads/foo"
    assert refs[1].local_ref == "refs/heads/bar"


def test_parse_stdin_blank_lines_are_skipped() -> None:
    lines = (
        "refs/heads/foo a1 refs/heads/foo a2\n"
        "\n"
        "  \n"
        "refs/heads/bar b1 refs/heads/bar b2\n"
    )
    refs = parse_stdin(_stdin(lines))
    assert len(refs) == 2


def test_parse_stdin_malformed_too_few_fields() -> None:
    with pytest.raises(ValueError):
        parse_stdin(_stdin("refs/heads/foo abc only-three-fields\n"))


def test_parse_stdin_malformed_too_many_fields() -> None:
    with pytest.raises(ValueError):
        parse_stdin(_stdin("a b c d e\n"))


def test_parse_stdin_malformed_single_token() -> None:
    with pytest.raises(ValueError):
        parse_stdin(_stdin("garbage\n"))


# --- RefUpdate properties --------------------------------------------------

def test_ref_update_is_delete_when_local_sha_zero() -> None:
    ref = RefUpdate("refs/heads/foo", "0" * 40, "refs/heads/foo", "abc123")
    assert ref.is_delete is True
    assert ref.is_new_branch is False  # remote_sha is non-zero


def test_ref_update_is_new_branch_when_remote_sha_zero() -> None:
    ref = RefUpdate("refs/heads/foo", "abc123", "refs/heads/foo", "0" * 40)
    assert ref.is_new_branch is True
    assert ref.is_delete is False


def test_ref_update_is_tag_when_ref_under_tags() -> None:
    ref = RefUpdate("refs/tags/v1.0", "abc", "refs/tags/v1.0", "def")
    assert ref.is_tag is True


def test_ref_update_not_tag_when_branch() -> None:
    ref = RefUpdate("refs/heads/main", "abc", "refs/heads/main", "def")
    assert ref.is_tag is False


def test_ref_update_tag_push_to_new_tag_is_not_new_branch() -> None:
    # New tag (zero remote_sha + refs/tags/* path): is_tag=True,
    # is_new_branch=False — the is_new_branch predicate explicitly
    # excludes tags so tag pushes don't trip the branch-creation logic.
    ref = RefUpdate("refs/tags/v1.0", "abc", "refs/tags/v1.0", "0" * 40)
    assert ref.is_tag is True
    assert ref.is_new_branch is False


# --- ReviewPayload derived properties --------------------------------------

def _review(changed: int, force: bool = False) -> RefReview:
    """Compact RefReview for tests."""
    ref = RefUpdate("refs/heads/x", "h", "refs/heads/x", "b")
    return RefReview(
        ref=ref, base_sha="b", base_label="origin", head_sha="h",
        is_force_push=force, commit_log="", diff="d", changed_lines=changed,
    )


def test_review_payload_empty_when_no_reviews() -> None:
    payload = ReviewPayload(reviews=[], skipped=[])
    assert payload.is_empty is True


def test_review_payload_non_empty_with_reviews() -> None:
    payload = ReviewPayload(reviews=[_review(10)], skipped=[])
    assert payload.is_empty is False


def test_review_payload_total_changed_lines() -> None:
    payload = ReviewPayload(reviews=[_review(10), _review(25), _review(7)], skipped=[])
    assert payload.total_changed_lines == 42


def test_review_payload_has_force_push_when_any() -> None:
    payload = ReviewPayload(
        reviews=[_review(10), _review(20, force=True), _review(30)],
        skipped=[],
    )
    assert payload.has_force_push is True


def test_review_payload_no_force_push_when_none() -> None:
    payload = ReviewPayload(reviews=[_review(10), _review(20)], skipped=[])
    assert payload.has_force_push is False


# --- format_diff_payload ---------------------------------------------------

def _full_review(
    *,
    remote_ref: str = "refs/heads/feature/x",
    new_branch: bool = False,
    force: bool = False,
    commit_log: str = "abc1234 Add foo",
    diff: str = "diff --git a/x b/x\n+pass\n",
    changed: int = 5,
) -> RefReview:
    local_sha = "h" * 40
    remote_sha = "0" * 40 if new_branch else "r" * 40
    ref = RefUpdate(remote_ref, local_sha, remote_ref, remote_sha)
    return RefReview(
        ref=ref, base_sha=remote_sha, base_label="origin (label)",
        head_sha=local_sha, is_force_push=force,
        commit_log=commit_log, diff=diff, changed_lines=changed,
    )


def test_format_diff_payload_includes_ref_header() -> None:
    payload = ReviewPayload(reviews=[_full_review()], skipped=[])
    text = format_diff_payload(payload)
    assert "=== ref: refs/heads/feature/x ===" in text


def test_format_diff_payload_marks_new_branch() -> None:
    payload = ReviewPayload(reviews=[_full_review(new_branch=True)], skipped=[])
    text = format_diff_payload(payload)
    assert "[new branch" in text


def test_format_diff_payload_marks_force_push() -> None:
    payload = ReviewPayload(reviews=[_full_review(force=True)], skipped=[])
    text = format_diff_payload(payload)
    assert "force-push" in text


def test_format_diff_payload_includes_commit_log() -> None:
    payload = ReviewPayload(
        reviews=[_full_review(commit_log="abc1234 First\ndef5678 Second")],
        skipped=[],
    )
    text = format_diff_payload(payload)
    assert "abc1234 First" in text
    assert "def5678 Second" in text


def test_format_diff_payload_includes_diff_body() -> None:
    payload = ReviewPayload(
        reviews=[_full_review(diff="diff --git a/x.py b/x.py\n+new\n")],
        skipped=[],
    )
    text = format_diff_payload(payload)
    assert "diff --git a/x.py b/x.py" in text
    assert "+new" in text


def test_format_diff_payload_includes_skipped_section() -> None:
    payload = ReviewPayload(
        reviews=[_full_review()],
        skipped=[("refs/heads/old", "branch deletion"),
                 ("refs/tags/v1", "tag (review_tags disabled)")],
    )
    text = format_diff_payload(payload)
    assert "=== skipped refs ===" in text
    assert "refs/heads/old: branch deletion" in text
    assert "refs/tags/v1: tag (review_tags disabled)" in text


def test_format_diff_payload_multiple_refs_separated() -> None:
    r1 = _full_review(remote_ref="refs/heads/a")
    r2 = _full_review(remote_ref="refs/heads/b")
    payload = ReviewPayload(reviews=[r1, r2], skipped=[])
    text = format_diff_payload(payload)
    assert "=== ref: refs/heads/a ===" in text
    assert "=== ref: refs/heads/b ===" in text
    # Multi-ref payloads are separated by blank lines.
    assert "\n\n" in text


# --- _diff_changed_files (real git plumbing) -------------------------------

def _init_repo(path: Path) -> None:
    """Initialize a tmp git repo with deterministic identity for tests.

    `diff.renames = true` is deliberately enabled so the rename test
    actually exercises the contract: `_diff_changed_files` MUST pass
    `--no-renames` itself rather than relying on the user's config to
    be permissive. If the helper ever drops `--no-renames`, the rename
    test will fail.
    """
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        # See docstring: enabled, not disabled — verifies the impl's
        # `--no-renames` flag overrides repo-level rename detection.
        ["git", "config", "diff.renames", "true"],
    ):
        subprocess.run(args, cwd=path, check=True, capture_output=True)


def _git_commit(path: Path, message: str) -> str:
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=path, check=True, capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def test_diff_changed_files_basic_modify(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True,
    )
    base = _git_commit(tmp_path, "init")
    (tmp_path / "a.txt").write_text("hello world\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True,
    )
    head = _git_commit(tmp_path, "edit")
    assert _diff_changed_files(base, head, tmp_path) == ["a.txt"]


def test_diff_changed_files_rename_emits_both_paths(tmp_path: Path) -> None:
    # Regression: when a gate's patterns match the source path of a
    # rename (e.g. `src/*` when a file moves out of src/), the source
    # path MUST appear in changed_files so the gate's all-or-nothing
    # check sees it. Default git rename detection only emits the
    # destination; `--no-renames` in `_diff_changed_files` makes the
    # rename appear as delete-old + add-new so both paths surface.
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True,
    )
    base = _git_commit(tmp_path, "init")
    (tmp_path / "lib").mkdir()
    subprocess.run(
        ["git", "mv", "src/foo.py", "lib/foo.py"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    head = _git_commit(tmp_path, "move")
    files = _diff_changed_files(base, head, tmp_path)
    assert sorted(files) == ["lib/foo.py", "src/foo.py"]


def test_diff_changed_files_rename_with_spaces_in_both_paths(
    tmp_path: Path,
) -> None:
    # Coverage gap from the previous push (called out by the tests
    # reviewer): a rename where BOTH the source and destination contain
    # spaces in their basenames. NUL-separated output should pass them
    # through verbatim with `--no-renames` emitting both as separate
    # entries.
    _init_repo(tmp_path)
    (tmp_path / "old name.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True,
    )
    base = _git_commit(tmp_path, "init")
    subprocess.run(
        ["git", "mv", "old name.py", "new name.py"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    head = _git_commit(tmp_path, "rename with spaces")
    files = _diff_changed_files(base, head, tmp_path)
    assert sorted(files) == ["new name.py", "old name.py"]


def test_diff_changed_files_path_with_space(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "weird file.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True,
    )
    base = _git_commit(tmp_path, "init")
    (tmp_path / "weird file.txt").write_text("b\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True,
    )
    head = _git_commit(tmp_path, "edit")
    assert _diff_changed_files(base, head, tmp_path) == ["weird file.txt"]
