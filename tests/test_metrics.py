"""Tests for src/metrics.py.

Covers the envelope usage extractor (`usage_from_stdout`) and the
cross-attempt aggregator (`combine`). These parse untrusted `claude -p`
output, so the contract is: never raise, default missing/ill-typed fields
to zero, and keep the four token counts as a split (never pre-summed).

The fixture envelope mirrors a real `claude -p --output-format json`
capture so the field names stay pinned to reality.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.aggregate import Finding, Verdict
from src.config import Config
from src.metrics import (
    ReviewerUsage,
    build_run_record,
    combine,
    format_summary,
    record_run,
    usage_from_stdout,
)


def _envelope(
    *,
    result: object = "ok",
    usage: dict | None = None,
    total_cost_usd: object = 0.0143173,
    duration_ms: object = 1873,
    is_error: bool = False,
    drop_usage: bool = False,
) -> str:
    """Build a claude -p envelope around a usage block, shaped like the
    real Phase 0 capture."""
    env: dict = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "duration_ms": duration_ms,
        "result": result if isinstance(result, str) else json.dumps(result),
        "total_cost_usd": total_cost_usd,
    }
    if not drop_usage:
        env["usage"] = usage if usage is not None else {
            "input_tokens": 9,
            "output_tokens": 66,
            "cache_creation_input_tokens": 10244,
            "cache_read_input_tokens": 7623,
        }
    return json.dumps(env)


# --- usage_from_stdout ------------------------------------------------------

def test_usage_from_stdout_happy_path() -> None:
    u = usage_from_stdout(_envelope(), agent_name="correctness")
    assert isinstance(u, ReviewerUsage)
    assert u.agent_name == "correctness"
    assert u.input_tokens == 9
    assert u.output_tokens == 66
    assert u.cache_creation_input_tokens == 10244
    assert u.cache_read_input_tokens == 7623
    assert u.cost_usd == 0.0143173
    assert u.duration_ms == 1873
    assert u.attempts == 1
    assert u.is_error is False


def test_usage_from_stdout_not_json_returns_none() -> None:
    # No envelope to read (subprocess crashed before emitting one).
    assert usage_from_stdout("not json at all", agent_name="x") is None


def test_usage_from_stdout_json_but_not_object_returns_none() -> None:
    assert usage_from_stdout(json.dumps([1, 2, 3]), agent_name="x") is None
    assert usage_from_stdout(json.dumps("a string"), agent_name="x") is None
    assert usage_from_stdout(json.dumps(None), agent_name="x") is None


def test_usage_from_stdout_missing_usage_block_zeros_tokens() -> None:
    # Envelope present but no `usage` key — tokens default to zero, but
    # cost/duration that live at the top level are still captured.
    u = usage_from_stdout(_envelope(drop_usage=True), agent_name="x")
    assert u is not None
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.cache_creation_input_tokens == 0
    assert u.cache_read_input_tokens == 0
    assert u.cost_usd == 0.0143173
    assert u.attempts == 1


def test_usage_from_stdout_usage_wrong_type_zeros_tokens() -> None:
    # `usage` present but not a dict (e.g. null) → treated as empty.
    env = json.dumps({"result": "ok", "usage": None, "total_cost_usd": 0.5})
    u = usage_from_stdout(env, agent_name="x")
    assert u is not None
    assert u.input_tokens == 0
    assert u.cost_usd == 0.5


def test_usage_from_stdout_missing_cost_defaults_zero() -> None:
    env = json.dumps({"result": "ok", "usage": {"input_tokens": 5}})
    u = usage_from_stdout(env, agent_name="x")
    assert u is not None
    assert u.cost_usd == 0.0
    assert u.input_tokens == 5


def test_usage_from_stdout_bool_token_value_rejected() -> None:
    # bool subclasses int; `"input_tokens": true` must NOT count as 1.
    # Mirrors the bool-vs-int guard in reviewer/config validators.
    u = usage_from_stdout(
        _envelope(usage={"input_tokens": True, "output_tokens": 3}),
        agent_name="x",
    )
    assert u is not None
    assert u.input_tokens == 0
    assert u.output_tokens == 3


def test_usage_from_stdout_string_token_value_ignored() -> None:
    u = usage_from_stdout(
        _envelope(usage={"input_tokens": "42", "output_tokens": 3}),
        agent_name="x",
    )
    assert u is not None
    assert u.input_tokens == 0
    assert u.output_tokens == 3


def test_usage_from_stdout_int_cost_coerced_to_float() -> None:
    u = usage_from_stdout(_envelope(total_cost_usd=1), agent_name="x")
    assert u is not None
    assert u.cost_usd == 1.0
    assert isinstance(u.cost_usd, float)


def test_usage_from_stdout_bool_cost_rejected() -> None:
    u = usage_from_stdout(_envelope(total_cost_usd=True), agent_name="x")
    assert u is not None
    assert u.cost_usd == 0.0


def test_usage_from_stdout_captures_error_envelope() -> None:
    # An error envelope (is_error=true) still cost tokens — capture it.
    u = usage_from_stdout(_envelope(is_error=True), agent_name="x")
    assert u is not None
    assert u.is_error is True
    assert u.input_tokens == 9


# --- combine ----------------------------------------------------------------

def test_combine_sums_all_numeric_fields() -> None:
    a = ReviewerUsage(
        agent_name="r",
        input_tokens=10, output_tokens=20,
        cache_creation_input_tokens=30, cache_read_input_tokens=40,
        cost_usd=0.5, duration_ms=100, attempts=1, is_error=False,
    )
    b = ReviewerUsage(
        agent_name="r",
        input_tokens=1, output_tokens=2,
        cache_creation_input_tokens=3, cache_read_input_tokens=4,
        cost_usd=0.25, duration_ms=200, attempts=1, is_error=False,
    )
    c = combine(a, b)
    assert c.agent_name == "r"
    assert c.input_tokens == 11
    assert c.output_tokens == 22
    assert c.cache_creation_input_tokens == 33
    assert c.cache_read_input_tokens == 44
    # Exact-representable binary fractions (0.5 + 0.25) so the equality is
    # not at the mercy of float rounding.
    assert c.cost_usd == 0.75
    assert c.duration_ms == 300
    assert c.attempts == 2
    assert c.is_error is False


def test_combine_is_error_true_if_either() -> None:
    a = ReviewerUsage(agent_name="r", is_error=True, attempts=1)
    b = ReviewerUsage(agent_name="r", is_error=False, attempts=1)
    assert combine(a, b).is_error is True
    assert combine(b, a).is_error is True


def test_combine_keeps_first_agent_name() -> None:
    a = ReviewerUsage(agent_name="first", attempts=1)
    b = ReviewerUsage(agent_name="second", attempts=1)
    assert combine(a, b).agent_name == "first"


# --- build_run_record -------------------------------------------------------

def _verdict(name: str, level: str = "PASS", findings: int = 0) -> Verdict:
    return Verdict(
        agent_name=name,
        verdict=level,  # type: ignore[arg-type]
        summary="s",
        reasoning="r",
        findings=[
            Finding(severity="warn", message=f"f{i}") for i in range(findings)
        ],
    )


def _usage(name: str, **kw) -> ReviewerUsage:
    return ReviewerUsage(agent_name=name, attempts=1, **kw)


def test_build_run_record_joins_usage_by_name() -> None:
    verdicts = [_verdict("a", "PASS", findings=1), _verdict("b", "WARN", findings=2)]
    usages = [
        _usage("a", input_tokens=10, cost_usd=0.01),
        _usage("b", input_tokens=20, cost_usd=0.02),
    ]
    rec = build_run_record(
        verdicts, usages,
        model="claude-sonnet-4-6", changed_lines=42, exit_code=0,
        timestamp="2026-06-17T00:00:00Z",
    )
    assert rec.schema_version == 1
    assert rec.model == "claude-sonnet-4-6"
    assert rec.changed_lines == 42
    assert rec.exit_code == 0
    by_name = {r.name: r for r in rec.reviewers}
    assert by_name["a"].input_tokens == 10
    assert by_name["a"].findings == 1
    assert by_name["b"].cost_usd == 0.02
    assert by_name["b"].findings == 2


def test_build_run_record_zero_usage_for_unmatched_verdict() -> None:
    # A synthetic verdict (missing persona / crash / meta-WARN) has no
    # matching usage → a zero row, so it appears without distorting totals.
    verdicts = [_verdict("ran"), _verdict("synthetic")]
    usages = [_usage("ran", input_tokens=5, cost_usd=0.5)]
    rec = build_run_record(
        verdicts, usages, model="m", changed_lines=1, exit_code=0,
    )
    by_name = {r.name: r for r in rec.reviewers}
    assert by_name["synthetic"].input_tokens == 0
    assert by_name["synthetic"].cost_usd == 0.0
    assert by_name["synthetic"].attempts == 0


def test_build_run_record_serializes_to_json() -> None:
    # asdict(record) must be JSON-serializable (no dataclass leaks) and
    # round-trip cleanly — this is exactly what gets written to the log.
    rec = build_run_record(
        [_verdict("a", findings=1)], [_usage("a", input_tokens=3)],
        model="m", changed_lines=1, exit_code=0,
        timestamp="2026-06-17T00:00:00Z",
    )
    from dataclasses import asdict
    parsed = json.loads(json.dumps(asdict(rec)))
    assert parsed["ts"] == "2026-06-17T00:00:00Z"
    assert parsed["reviewers"][0]["name"] == "a"
    assert parsed["reviewers"][0]["input_tokens"] == 3


# --- format_summary ---------------------------------------------------------

def test_format_summary_is_ascii_and_has_key_numbers() -> None:
    rec = build_run_record(
        [_verdict("a"), _verdict("b")],
        [
            _usage("a", input_tokens=1000, cache_read_input_tokens=3000),
            _usage("b", input_tokens=1000, cache_creation_input_tokens=1000),
        ],
        model="m", changed_lines=1, exit_code=0,
    )
    s = format_summary(rec)
    s.encode("ascii")  # must not raise — ASCII-only for cp1252 stderr safety
    assert "2 reviewers" in s
    assert "cached" in s
    assert "$" in s


def test_format_summary_cache_ratio_zero_division_safe() -> None:
    # All-synthetic run: no tokens at all. Must not raise on the cache %.
    rec = build_run_record(
        [_verdict("ghost")], [], model="m", changed_lines=0, exit_code=0,
    )
    s = format_summary(rec)
    assert "0%" in s
    assert "1 reviewer," in s  # singular


# --- record_run (persistence) ----------------------------------------------

def _config(repo_root: Path, **overrides) -> Config:
    defaults: dict = dict(
        spec_path=Path("CLAUDE.md"),
        default_branch="",
        enabled_personas=["a"],
        model="claude-sonnet-4-6",
        parallel=False,
        review_tags=False,
        override_env="CLAUDE_MULTI_AGENT_REVIEW_OVERRIDE",
        reviewer_timeout_seconds=180,
        reviewer_retries=1,
        treat_reviewer_failure_as="warn",
        max_diff_lines=5000,
        install_root=repo_root,
        repo_root=repo_root,
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_record_run_appends_jsonl_line(tmp_path: Path) -> None:
    cfg = _config(tmp_path, metrics_path="m.jsonl")
    summary = record_run(
        cfg, [_verdict("a", findings=1)], [_usage("a", input_tokens=7, cost_usd=0.1)],
        changed_lines=12, exit_code=0,
    )
    log = tmp_path / "m.jsonl"
    assert log.is_file()
    line = log.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["changed_lines"] == 12
    assert rec["reviewers"][0]["input_tokens"] == 7
    assert "claude-multi-agent-review:" in summary


def test_record_run_appends_not_overwrites(tmp_path: Path) -> None:
    cfg = _config(tmp_path, metrics_path="m.jsonl")
    record_run(cfg, [_verdict("a")], [_usage("a")], changed_lines=1, exit_code=0)
    record_run(cfg, [_verdict("a")], [_usage("a")], changed_lines=2, exit_code=0)
    lines = (tmp_path / "m.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["changed_lines"] == 1
    assert json.loads(lines[1])["changed_lines"] == 2


def test_record_run_creates_parent_dir(tmp_path: Path) -> None:
    cfg = _config(tmp_path, metrics_path="nested/dir/m.jsonl")
    record_run(cfg, [_verdict("a")], [_usage("a")], changed_lines=1, exit_code=0)
    assert (tmp_path / "nested" / "dir" / "m.jsonl").is_file()


def test_record_run_adds_path_to_gitignore(tmp_path: Path) -> None:
    cfg = _config(tmp_path, metrics_path=".cmar/metrics.jsonl")
    record_run(cfg, [_verdict("a")], [_usage("a")], changed_lines=1, exit_code=0)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".cmar/metrics.jsonl" in gitignore.splitlines()


def test_record_run_tolerates_non_utf8_gitignore(tmp_path: Path) -> None:
    # An existing .gitignore with non-UTF-8 bytes must not crash record_run.
    # read_text(encoding="utf-8") raises UnicodeDecodeError (a ValueError, not
    # an OSError); if it escaped it would hit the top-level handler and flip
    # the push to exit 2 — potentially allowing a push reviewers just blocked
    # (Codex P2 on #19). The metrics line must still be written.
    (tmp_path / ".gitignore").write_bytes(b"\xff\xfe not valid utf-8\n")
    cfg = _config(tmp_path, metrics_path="m.jsonl")
    summary = record_run(
        cfg, [_verdict("a")], [_usage("a")], changed_lines=1, exit_code=0,
    )
    assert "claude-multi-agent-review:" in summary
    assert (tmp_path / "m.jsonl").is_file()  # write happened despite decode fail


def test_record_run_refuses_symlinked_gitignore_escaping_repo(
    tmp_path: Path,
) -> None:
    # If .gitignore is a symlink to a file OUTSIDE the repo, _ensure_gitignored
    # must not follow it and append to that external file (Codex P2 on #19).
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external.gitignore"
    external.write_text("original\n", encoding="utf-8")
    try:
        (repo / ".gitignore").symlink_to(external)
    except (OSError, NotImplementedError) as e:
        # Technical blocker: creating symlinks needs privilege/Developer Mode
        # on Windows; the production guard is platform-independent regardless.
        pytest.skip(f"symlink creation unsupported in this environment: {e}")

    cfg = _config(repo, metrics_path="m.jsonl")
    summary = record_run(
        cfg, [_verdict("a")], [_usage("a")], changed_lines=1, exit_code=0,
    )
    # The external file was NOT mutated...
    assert external.read_text(encoding="utf-8") == "original\n"
    # ...and the metrics line was still written (the guard is gitignore-only).
    assert (repo / "m.jsonl").is_file()
    assert "claude-multi-agent-review:" in summary


def test_record_run_gitignore_idempotent(tmp_path: Path) -> None:
    cfg = _config(tmp_path, metrics_path=".cmar/metrics.jsonl")
    record_run(cfg, [_verdict("a")], [_usage("a")], changed_lines=1, exit_code=0)
    record_run(cfg, [_verdict("a")], [_usage("a")], changed_lines=2, exit_code=0)
    entries = [
        ln for ln in (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
        if ln.strip() == ".cmar/metrics.jsonl"
    ]
    assert len(entries) == 1  # not appended twice


def test_record_run_preserves_existing_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc", encoding="utf-8")
    cfg = _config(tmp_path, metrics_path=".cmar/metrics.jsonl")
    record_run(cfg, [_verdict("a")], [_usage("a")], changed_lines=1, exit_code=0)
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "__pycache__/" in lines
    assert "*.pyc" in lines  # not mangled despite missing trailing newline
    assert ".cmar/metrics.jsonl" in lines


def test_record_run_rejects_path_outside_repo(tmp_path: Path, capsys) -> None:
    # metrics_path escaping the repo root must be refused (no write) but the
    # summary still returns so the per-run line shows.
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _config(repo, metrics_path="../escape.jsonl")
    summary = record_run(
        cfg, [_verdict("a")], [_usage("a")], changed_lines=1, exit_code=0,
    )
    assert not (tmp_path / "escape.jsonl").exists()
    assert "claude-multi-agent-review:" in summary
    assert "outside the repo" in capsys.readouterr().err


def test_record_run_write_failure_is_best_effort(tmp_path: Path, capsys) -> None:
    # If the target path can't be written (here: a directory exists where
    # the file should be), record_run must not raise — it degrades to a
    # stderr notice and still returns the summary.
    cfg = _config(tmp_path, metrics_path="m.jsonl")
    (tmp_path / "m.jsonl").mkdir()  # collide: open('a') on a dir → OSError
    summary = record_run(
        cfg, [_verdict("a")], [_usage("a")], changed_lines=1, exit_code=0,
    )
    assert "claude-multi-agent-review:" in summary
    assert "could not write metrics" in capsys.readouterr().err
