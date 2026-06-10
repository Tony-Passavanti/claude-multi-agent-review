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
from src.config import Config
from src.orchestrate import (
    _diff_size_meta_verdict,
    _is_within,
    _read_spec,
    _resolve_persona_path,
    _reviewer_crashed_verdict,
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
