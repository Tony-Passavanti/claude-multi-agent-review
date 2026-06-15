"""Tests for src/orchestrate.py.

Covers the dispatcher (`review_all` with injected `reviewer_fn`), the
persona-resolution helpers (`_resolve_persona_path`, `_is_within`), the
spec reader (`_read_spec`), and the synthetic-verdict factories
(`_diff_size_meta_verdict`, `_synthetic_missing_persona_verdict`,
`_reviewer_crashed_verdict`).

The injectable `reviewer_fn` parameter on `review_all` is what makes
these tests possible without a live `claude` CLI: we pass a mock
function that returns a Verdict directly, and assert on what the
orchestrator does with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from src import hook
from src.aggregate import Verdict
from src.config import Config, ReviewerGate
from src.orchestrate import (
    _changed_files_from_payload,
    _diff_size_meta_verdict,
    _gate_covers_all,
    _is_within,
    _read_spec,
    _resolve_persona_path,
    _reviewer_crashed_verdict,
    _select_personas,
    _synthetic_missing_persona_verdict,
    review_all,
)


# --- helpers ---------------------------------------------------------------

def _verdict(name: str, level: str = "PASS", summary: str = "ok") -> Verdict:
    return Verdict(
        agent_name=name,
        verdict=level,  # type: ignore[arg-type]
        summary=summary,
        reasoning="r",
        findings=[],
    )


def _ref(local_sha: str = "h" * 40, remote_sha: str = "r" * 40) -> hook.RefUpdate:
    return hook.RefUpdate(
        local_ref="refs/heads/feature/x",
        local_sha=local_sha,
        remote_ref="refs/heads/feature/x",
        remote_sha=remote_sha,
    )


def _review(changed_lines: int = 5) -> hook.RefReview:
    return hook.RefReview(
        ref=_ref(),
        base_sha="r" * 40,
        base_label="origin/x",
        head_sha="h" * 40,
        is_force_push=False,
        commit_log="abc1234 Add feature",
        diff="diff --git a/x b/x\n+pass\n",
        changed_lines=changed_lines,
    )


def _payload(changed: int = 5) -> hook.ReviewPayload:
    return hook.ReviewPayload(reviews=[_review(changed_lines=changed)], skipped=[])


def _payload_with_files(*paths: str) -> hook.ReviewPayload:
    """Build a ReviewPayload whose diff names exactly these changed files."""
    diff = "".join(f"diff --git a/{p} b/{p}\n+x\n" for p in paths)
    review = hook.RefReview(
        ref=_ref(), base_sha="r" * 40, base_label="origin/x",
        head_sha="h" * 40, is_force_push=False,
        commit_log="", diff=diff, changed_lines=len(paths),
    )
    return hook.ReviewPayload(reviews=[review], skipped=[])


def _mock_reviewer(
    *, persona_name: str, persona_path: Path, spec: str,
    diff_payload: str, config: Config, log: Callable[[str], None] | None = None,
) -> Verdict:
    """A stand-in for reviewer.review that returns PASS without invoking claude."""
    return _verdict(persona_name, "PASS", f"mock review of {persona_name}")


# --- _is_within ------------------------------------------------------------

def test_is_within_child(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    child = (tmp_path / "sub" / "file.md").resolve()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.md").write_text("x")
    assert _is_within(child, root) is True


def test_is_within_self(tmp_path: Path) -> None:
    assert _is_within(tmp_path.resolve(), tmp_path.resolve()) is True


def test_is_within_rejects_sibling(tmp_path: Path) -> None:
    sibling = (tmp_path.parent / "other").resolve()
    assert _is_within(sibling, tmp_path.resolve()) is False


def test_is_within_rejects_parent(tmp_path: Path) -> None:
    parent = tmp_path.parent.resolve()
    assert _is_within(parent, tmp_path.resolve()) is False


# --- _resolve_persona_path -------------------------------------------------

def test_resolve_persona_uses_shipped_when_no_local(
    make_persona, make_config,
) -> None:
    shipped = make_persona("security")
    cfg = make_config()
    found = _resolve_persona_path("security", cfg)
    assert found == shipped.resolve()


def test_resolve_persona_prefers_repo_local_override(
    make_persona, make_local_persona, make_config,
) -> None:
    make_persona("security", content="shipped content")
    local = make_local_persona("security", content="repo-local content")
    cfg = make_config()
    found = _resolve_persona_path("security", cfg)
    assert found == local.resolve()


def test_resolve_persona_returns_none_when_missing(make_config) -> None:
    cfg = make_config()
    assert _resolve_persona_path("nonexistent", cfg) is None


def test_resolve_persona_rejects_path_traversal_upward(make_config) -> None:
    # A persona name that tries to escape via ../../ should not resolve
    # to a file outside the persona root, even if such a file exists.
    cfg = make_config()
    assert _resolve_persona_path("../../../etc/passwd", cfg) is None


def test_resolve_persona_rejects_absolute_path(make_config) -> None:
    cfg = make_config()
    # Backslashes here are OK on Windows; Path normalizes them.
    assert _resolve_persona_path("/etc/shadow", cfg) is None


# --- _read_spec ------------------------------------------------------------

def test_read_spec_happy_path(make_config) -> None:
    cfg = make_config()
    spec = _read_spec(cfg)
    assert "Test spec" in spec


def test_read_spec_missing_file_raises(make_config, repo_root: Path) -> None:
    (repo_root / "CLAUDE.md").unlink()
    cfg = make_config()
    with pytest.raises(FileNotFoundError, match="spec file not found"):
        _read_spec(cfg)


def test_read_spec_rejects_outside_repo_root(make_config) -> None:
    # A spec_path that resolves outside the repo root is a traversal
    # attempt — must be rejected before any file read happens.
    cfg = make_config(spec_path=Path("../../../etc/passwd"))
    with pytest.raises(FileNotFoundError, match="path-traversal"):
        _read_spec(cfg)


def test_read_spec_accepts_subdirectory_path(
    make_config, repo_root: Path,
) -> None:
    # spec_path = "docs/SPEC.md" resolves under repo_root → ok.
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "SPEC.md").write_text("# docs spec\n", encoding="utf-8")
    cfg = make_config(spec_path=Path("docs/SPEC.md"))
    spec = _read_spec(cfg)
    assert "docs spec" in spec


# --- synthetic verdicts ----------------------------------------------------

def test_diff_size_meta_verdict_content() -> None:
    payload = _payload(changed=6000)
    v = _diff_size_meta_verdict(payload, max_lines=5000)
    assert v.agent_name == "claude-multi-agent-review"
    assert v.verdict == "WARN"
    assert "6000" in v.summary
    assert "5000" in v.summary
    assert v.findings == []


def test_synthetic_missing_persona_verdict_warn_mode() -> None:
    v = _synthetic_missing_persona_verdict("ghost", mode="warn")
    assert v.agent_name == "ghost"
    assert v.verdict == "WARN"
    assert "ghost" in v.summary
    assert v.findings == []


def test_synthetic_missing_persona_verdict_fail_mode() -> None:
    v = _synthetic_missing_persona_verdict("ghost", mode="fail")
    assert v.verdict == "FAIL"


def test_reviewer_crashed_verdict_warn_mode() -> None:
    exc = ValueError("boom")
    v = _reviewer_crashed_verdict("security", exc, mode="warn")
    assert v.agent_name == "security"
    assert v.verdict == "WARN"
    assert "ValueError" in v.summary
    assert "boom" in v.reasoning


def test_reviewer_crashed_verdict_fail_mode() -> None:
    exc = ValueError("boom")
    v = _reviewer_crashed_verdict("security", exc, mode="fail")
    assert v.verdict == "FAIL"


# --- review_all happy path -------------------------------------------------

def test_review_all_sequential_happy_path(
    make_persona, make_config,
) -> None:
    make_persona("architecture")
    make_persona("security")
    cfg = make_config(
        enabled_personas=["architecture", "security"],
        parallel=False,
    )
    verdicts = review_all(_payload(), cfg, reviewer_fn=_mock_reviewer)
    assert len(verdicts) == 2
    assert {v.agent_name for v in verdicts} == {"architecture", "security"}
    assert all(v.verdict == "PASS" for v in verdicts)


def test_review_all_parallel_happy_path(make_persona, make_config) -> None:
    for name in ("a", "b", "c"):
        make_persona(name)
    cfg = make_config(enabled_personas=["a", "b", "c"], parallel=True)
    verdicts = review_all(_payload(), cfg, reviewer_fn=_mock_reviewer)
    assert len(verdicts) == 3
    assert {v.agent_name for v in verdicts} == {"a", "b", "c"}


def test_review_all_sequential_preserves_order(
    make_persona, make_config,
) -> None:
    for name in ("first", "second", "third"):
        make_persona(name)
    cfg = make_config(
        enabled_personas=["first", "second", "third"],
        parallel=False,
    )
    verdicts = review_all(_payload(), cfg, reviewer_fn=_mock_reviewer)
    # Sequential dispatch returns in submission order.
    assert [v.agent_name for v in verdicts] == ["first", "second", "third"]


# --- review_all: missing personas → synthetic verdicts --------------------

def test_review_all_missing_persona_yields_synthetic_warn(
    make_persona, make_config,
) -> None:
    make_persona("real")  # ships
    # 'ghost' has no persona file
    cfg = make_config(
        enabled_personas=["real", "ghost"],
        treat_reviewer_failure_as="warn",
    )
    verdicts = review_all(_payload(), cfg, reviewer_fn=_mock_reviewer)
    by_name = {v.agent_name: v for v in verdicts}
    assert by_name["real"].verdict == "PASS"
    assert by_name["ghost"].verdict == "WARN"
    assert "not found" in by_name["ghost"].summary


def test_review_all_missing_persona_fail_mode(
    make_persona, make_config,
) -> None:
    cfg = make_config(
        enabled_personas=["ghost"],
        treat_reviewer_failure_as="fail",
    )
    verdicts = review_all(_payload(), cfg, reviewer_fn=_mock_reviewer)
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "FAIL"


def test_review_all_all_personas_missing(make_config) -> None:
    # No personas exist at all. All resolve to synthetic verdicts;
    # no actual dispatch happens.
    cfg = make_config(enabled_personas=["ghost1", "ghost2"])
    called = []
    def tracking_reviewer(**kwargs):
        called.append(kwargs["persona_name"])
        return _verdict(kwargs["persona_name"])
    verdicts = review_all(_payload(), cfg, reviewer_fn=tracking_reviewer)
    assert len(verdicts) == 2
    assert all(v.verdict == "WARN" for v in verdicts)
    assert called == []  # reviewer was never invoked


# --- review_all: oversized diff produces meta-WARN ------------------------

def test_review_all_oversized_diff_emits_meta_warn(
    make_persona, make_config,
) -> None:
    make_persona("only")
    cfg = make_config(
        enabled_personas=["only"],
        max_diff_lines=100,
    )
    verdicts = review_all(
        _payload(changed=5000), cfg, reviewer_fn=_mock_reviewer,
    )
    # Should have both the meta-WARN AND the real verdict
    by_name = {v.agent_name: v for v in verdicts}
    assert "claude-multi-agent-review" in by_name
    assert by_name["claude-multi-agent-review"].verdict == "WARN"
    assert "only" in by_name
    assert by_name["only"].verdict == "PASS"


def test_review_all_under_threshold_no_meta_warn(
    make_persona, make_config,
) -> None:
    make_persona("only")
    cfg = make_config(enabled_personas=["only"], max_diff_lines=10_000)
    verdicts = review_all(
        _payload(changed=42), cfg, reviewer_fn=_mock_reviewer,
    )
    assert all(v.agent_name != "claude-multi-agent-review" for v in verdicts)


# --- review_all: reviewer crash safety net --------------------------------

def test_review_all_catches_reviewer_exception_sequential(
    make_persona, make_config,
) -> None:
    make_persona("flaky")
    cfg = make_config(enabled_personas=["flaky"], parallel=False)
    def crashing_reviewer(**kwargs):
        raise RuntimeError("simulated crash")
    verdicts = review_all(_payload(), cfg, reviewer_fn=crashing_reviewer)
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "WARN"
    assert "crashed" in verdicts[0].summary
    assert "RuntimeError" in verdicts[0].summary


def test_review_all_catches_reviewer_exception_parallel(
    make_persona, make_config,
) -> None:
    # Two personas required to actually exercise `_dispatch_parallel`:
    # review_all() falls back to sequential when len(real_jobs) <= 1
    # regardless of config.parallel. With one persona, this test would
    # silently exercise the sequential path and miss any regression in
    # the parallel branch's ThreadPoolExecutor exception handling.
    make_persona("flaky1")
    make_persona("flaky2")
    cfg = make_config(enabled_personas=["flaky1", "flaky2"], parallel=True)
    def crashing_reviewer(**kwargs):
        raise RuntimeError("simulated crash")
    verdicts = review_all(_payload(), cfg, reviewer_fn=crashing_reviewer)
    assert len(verdicts) == 2
    assert all(v.verdict == "WARN" for v in verdicts)
    assert all("crashed" in v.summary for v in verdicts)


def test_review_all_parallel_one_crash_does_not_block_others(
    make_persona, make_config,
) -> None:
    # Same property as the sequential `one_crash_does_not_block_other_reviewers`
    # test, but verifying it holds on the parallel dispatch path too —
    # `as_completed`'s per-future exception handling must isolate crashes.
    make_persona("ok1")
    make_persona("ok2")
    make_persona("flaky")
    cfg = make_config(
        enabled_personas=["ok1", "ok2", "flaky"], parallel=True,
    )
    def selective_crasher(**kwargs):
        if kwargs["persona_name"] == "flaky":
            raise RuntimeError("just this one")
        return _verdict(kwargs["persona_name"])
    verdicts = review_all(_payload(), cfg, reviewer_fn=selective_crasher)
    assert len(verdicts) == 3
    by_name = {v.agent_name: v for v in verdicts}
    assert by_name["ok1"].verdict == "PASS"
    assert by_name["ok2"].verdict == "PASS"
    assert by_name["flaky"].verdict == "WARN"
    assert "crashed" in by_name["flaky"].summary


def test_review_all_one_crash_does_not_block_other_reviewers(
    make_persona, make_config,
) -> None:
    make_persona("ok")
    make_persona("flaky")
    cfg = make_config(enabled_personas=["ok", "flaky"], parallel=False)
    def selective_crasher(**kwargs):
        if kwargs["persona_name"] == "flaky":
            raise RuntimeError("just this one")
        return _verdict(kwargs["persona_name"])
    verdicts = review_all(_payload(), cfg, reviewer_fn=selective_crasher)
    assert len(verdicts) == 2
    by_name = {v.agent_name: v for v in verdicts}
    assert by_name["ok"].verdict == "PASS"
    assert by_name["flaky"].verdict == "WARN"
    assert "crashed" in by_name["flaky"].summary


# --- review_all: spec failures propagate ----------------------------------

def test_review_all_raises_when_spec_missing(
    make_persona, make_config, repo_root: Path,
) -> None:
    make_persona("only")
    (repo_root / "CLAUDE.md").unlink()
    cfg = make_config(enabled_personas=["only"])
    with pytest.raises(FileNotFoundError):
        review_all(_payload(), cfg, reviewer_fn=_mock_reviewer)


# --- review_all: parallel falls back to sequential when N == 1 ------------

def test_review_all_single_reviewer_uses_sequential_path(
    make_persona, make_config,
) -> None:
    # With only one real reviewer, the parallel/sequential branch
    # should pick sequential (no need for a ThreadPoolExecutor).
    # This is hard to observe directly; we verify it via behavior:
    # the result is correct regardless of which branch ran.
    make_persona("solo")
    cfg = make_config(enabled_personas=["solo"], parallel=True)
    verdicts = review_all(_payload(), cfg, reviewer_fn=_mock_reviewer)
    assert len(verdicts) == 1
    assert verdicts[0].agent_name == "solo"


# --- review_all: reviewer receives expected arguments ---------------------

def test_review_all_passes_spec_and_diff_to_reviewer(
    make_persona, make_config,
) -> None:
    make_persona("inspector")
    cfg = make_config(enabled_personas=["inspector"])
    received: dict = {}
    def capturing_reviewer(**kwargs):
        received.update(kwargs)
        return _verdict(kwargs["persona_name"])
    review_all(_payload(), cfg, reviewer_fn=capturing_reviewer)
    assert received["persona_name"] == "inspector"
    assert "Test spec" in received["spec"]
    assert "diff --git" in received["diff_payload"]
    assert received["config"] is cfg
    assert callable(received["log"])  # orchestrator passes its lock-aware logger


# --- changed-file extraction ----------------------------------------------

def test_changed_files_from_payload_multiple_refs() -> None:
    payload = _payload_with_files("src/foo.py", "docs/readme.md")
    assert _changed_files_from_payload(payload) == [
        "docs/readme.md", "src/foo.py",
    ]


def test_changed_files_from_payload_rename_includes_both_sides() -> None:
    review = hook.RefReview(
        ref=_ref(), base_sha="r" * 40, base_label="b",
        head_sha="h" * 40, is_force_push=False, commit_log="",
        diff="diff --git a/old/name.py b/new/name.py\nsimilarity index 100%\n",
        changed_lines=0,
    )
    payload = hook.ReviewPayload(reviews=[review], skipped=[])
    assert _changed_files_from_payload(payload) == [
        "new/name.py", "old/name.py",
    ]


def test_changed_files_from_payload_quoted_path_with_space() -> None:
    review = hook.RefReview(
        ref=_ref(), base_sha="r" * 40, base_label="b",
        head_sha="h" * 40, is_force_push=False, commit_log="",
        diff='diff --git "a/dir with space/x.md" "b/dir with space/x.md"\n',
        changed_lines=0,
    )
    payload = hook.ReviewPayload(reviews=[review], skipped=[])
    assert _changed_files_from_payload(payload) == ["dir with space/x.md"]


def test_changed_files_from_payload_empty() -> None:
    payload = hook.ReviewPayload(reviews=[], skipped=[])
    assert _changed_files_from_payload(payload) == []


# --- _gate_covers_all -----------------------------------------------------

def test_gate_covers_all_true_when_every_file_matches() -> None:
    gate = ReviewerGate(name="docs", patterns=["*.md"], personas=["a"])
    assert _gate_covers_all(gate, ["README.md", "docs/x.md"]) is True


def test_gate_covers_all_false_when_one_file_unmatched() -> None:
    # All-or-nothing: a single unmatched file blocks the gate.
    gate = ReviewerGate(name="docs", patterns=["*.md"], personas=["a"])
    assert _gate_covers_all(gate, ["README.md", "src/main.py"]) is False


def test_gate_covers_all_multiple_patterns_union() -> None:
    gate = ReviewerGate(
        name="docs", patterns=["*.md", "*.txt"], personas=["a"],
    )
    assert _gate_covers_all(gate, ["a.md", "NOTES.txt"]) is True


# --- _select_personas -----------------------------------------------------

def test_select_personas_no_gates_configured(make_config) -> None:
    cfg = make_config(enabled_personas=["a", "b", "c"])
    payload = _payload_with_files("anything.py")
    personas, gate = _select_personas(payload, cfg)
    assert personas == ["a", "b", "c"]
    assert gate is None


def test_select_personas_gate_matches_narrows_subset(make_config) -> None:
    gate = ReviewerGate(name="docs", patterns=["*.md"], personas=["a", "c"])
    cfg = make_config(
        enabled_personas=["a", "b", "c"],
        reviewer_gates=[gate],
    )
    payload = _payload_with_files("README.md", "docs/x.md")
    personas, fired = _select_personas(payload, cfg)
    assert personas == ["a", "c"]
    assert fired is gate


def test_select_personas_mixed_content_skips_all_gates(make_config) -> None:
    # Even one non-matching file means the gate does not apply — the user
    # gets the full enabled set rather than a misleading narrowed review.
    gate = ReviewerGate(name="docs", patterns=["*.md"], personas=["a"])
    cfg = make_config(
        enabled_personas=["a", "b"],
        reviewer_gates=[gate],
    )
    payload = _payload_with_files("docs/x.md", "src/foo.py")
    personas, fired = _select_personas(payload, cfg)
    assert personas == ["a", "b"]
    assert fired is None


def test_select_personas_first_matching_gate_wins(make_config) -> None:
    g1 = ReviewerGate(name="docs", patterns=["*.md"], personas=["a"])
    g2 = ReviewerGate(name="catchall", patterns=["*"], personas=["b"])
    cfg = make_config(
        enabled_personas=["a", "b"],
        reviewer_gates=[g1, g2],
    )
    payload = _payload_with_files("README.md")
    personas, fired = _select_personas(payload, cfg)
    assert fired is g1
    assert personas == ["a"]


def test_select_personas_intersects_with_enabled(make_config) -> None:
    # A gate cannot resurrect a globally disabled persona.
    gate = ReviewerGate(
        name="docs", patterns=["*.md"],
        personas=["a", "disabled_one"],
    )
    cfg = make_config(
        enabled_personas=["a", "b"],
        reviewer_gates=[gate],
    )
    payload = _payload_with_files("README.md")
    personas, fired = _select_personas(payload, cfg)
    assert fired is gate
    assert personas == ["a"]


def test_select_personas_preserves_enabled_order(make_config) -> None:
    # Selected subset is iterated in `enabled_personas` order, not in
    # the gate's `personas` order — keeps streaming output stable.
    gate = ReviewerGate(
        name="docs", patterns=["*.md"], personas=["c", "a"],
    )
    cfg = make_config(
        enabled_personas=["a", "b", "c"],
        reviewer_gates=[gate],
    )
    payload = _payload_with_files("README.md")
    personas, _ = _select_personas(payload, cfg)
    assert personas == ["a", "c"]


def test_select_personas_empty_payload_falls_back_to_enabled(
    make_config,
) -> None:
    gate = ReviewerGate(name="docs", patterns=["*.md"], personas=["a"])
    cfg = make_config(
        enabled_personas=["a", "b"],
        reviewer_gates=[gate],
    )
    payload = hook.ReviewPayload(reviews=[], skipped=[])
    personas, fired = _select_personas(payload, cfg)
    assert personas == ["a", "b"]
    assert fired is None


# --- review_all with reviewer gates --------------------------------------

def test_review_all_gate_narrows_dispatch(
    make_persona, make_config,
) -> None:
    for name in ("spec_conformance", "agent_drift", "architecture"):
        make_persona(name)
    gate = ReviewerGate(
        name="docs-only",
        patterns=["*.md"],
        personas=["spec_conformance", "agent_drift"],
    )
    cfg = make_config(
        enabled_personas=["spec_conformance", "architecture", "agent_drift"],
        reviewer_gates=[gate],
    )
    verdicts = review_all(
        _payload_with_files("README.md", "docs/guide.md"),
        cfg, reviewer_fn=_mock_reviewer,
    )
    assert {v.agent_name for v in verdicts} == {
        "spec_conformance", "agent_drift",
    }


def test_review_all_no_gate_match_runs_full_set(
    make_persona, make_config,
) -> None:
    make_persona("a")
    make_persona("b")
    gate = ReviewerGate(name="docs", patterns=["*.md"], personas=["a"])
    cfg = make_config(
        enabled_personas=["a", "b"],
        reviewer_gates=[gate],
    )
    verdicts = review_all(
        _payload_with_files("src/foo.py"),
        cfg, reviewer_fn=_mock_reviewer,
    )
    assert {v.agent_name for v in verdicts} == {"a", "b"}


def test_review_all_header_announces_matched_gate(
    make_persona, make_config, capsys,
) -> None:
    make_persona("a")
    gate = ReviewerGate(name="docs-only", patterns=["*.md"], personas=["a"])
    cfg = make_config(
        enabled_personas=["a", "b"],  # "b" intentionally has no file
        reviewer_gates=[gate],
    )
    review_all(
        _payload_with_files("README.md"),
        cfg, reviewer_fn=_mock_reviewer,
    )
    err = capsys.readouterr().err
    assert "docs-only" in err
    assert "1 of 2" in err


def test_review_all_header_announces_no_gate_matched(
    make_persona, make_config, capsys,
) -> None:
    make_persona("a")
    gate = ReviewerGate(name="docs-only", patterns=["*.md"], personas=["a"])
    cfg = make_config(
        enabled_personas=["a"],
        reviewer_gates=[gate],
    )
    review_all(
        _payload_with_files("src/foo.py"),
        cfg, reviewer_fn=_mock_reviewer,
    )
    err = capsys.readouterr().err
    assert "no gate matched" in err


def test_review_all_gate_skipped_personas_are_not_invoked(
    make_persona, make_config,
) -> None:
    # Verify the gated-out persona's reviewer_fn is NOT called — the
    # whole point of the feature is to save token cost.
    make_persona("kept")
    make_persona("skipped")
    gate = ReviewerGate(name="docs", patterns=["*.md"], personas=["kept"])
    cfg = make_config(
        enabled_personas=["kept", "skipped"],
        reviewer_gates=[gate],
    )
    called: list[str] = []
    def tracking_reviewer(**kwargs):
        called.append(kwargs["persona_name"])
        return _verdict(kwargs["persona_name"])
    review_all(
        _payload_with_files("README.md"),
        cfg, reviewer_fn=tracking_reviewer,
    )
    assert called == ["kept"]
