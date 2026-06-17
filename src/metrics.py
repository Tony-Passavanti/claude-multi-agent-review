"""Per-reviewer token/cost usage capture and per-run metrics persistence.

Two layers:

  1. Capture (issue #18): parse token counts and actual cost out of the
     `claude -p --output-format json` envelope (`ReviewerUsage`,
     `usage_from_stdout`, `combine`).

  2. Persist (issue #19): assemble one `RunRecord` per push and append it
     to a JSONL log, plus format the one-line per-run summary
     (`build_run_record`, `record_run`, `format_summary`). Cumulative
     reporting over that log is Phase 3 (issue #20).

Three deliberate properties:

  - The capture helpers never raise on malformed input. A missing or
    wrong-typed `usage` block yields a zero-valued ReviewerUsage (or None
    when there is no envelope at all). `reviewer.review()` depends on this
    to honor CLAUDE.md errors.reviewer-never-raises-on-failure.

  - Persistence is best-effort: a failed write (read-only FS, full disk,
    permissions) degrades to a one-line stderr notice and NEVER changes
    the push outcome, which is decided solely by reviewer verdicts.

  - Token counts are kept as a four-way split and never pre-summed, so
    cache effectiveness — cache_read / (cache_read + cache_creation +
    input) — can be computed downstream. `cost_usd` is the CLI's
    authoritative `total_cost_usd`, not an estimate.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .aggregate import Verdict
    from .config import Config

# Bumped when the JSONL record shape changes incompatibly, so the Phase 3
# stats reader (issue #20) can tell old lines from new.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReviewerUsage:
    """Resource usage for one reviewer across all of its claude -p attempts.

    `attempts` counts the billable model calls we captured usage for, so a
    parse-retry (which makes a second `claude -p` call) adds one. This can
    differ from the retry loop's attempt count: attempts that fail before
    emitting an envelope (timeout, non-zero exit) contribute no usage and
    are not counted here.
    """
    agent_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    attempts: int = 0
    is_error: bool = False


def usage_from_stdout(stdout: str, *, agent_name: str) -> ReviewerUsage | None:
    """Extract a ReviewerUsage from one `claude -p` JSON envelope.

    Returns None when `stdout` is not a JSON object — there is no envelope
    to read (e.g. the subprocess crashed before emitting one), so there is
    nothing to attribute. Otherwise returns a ReviewerUsage carrying
    whatever fields are present, defaulting each missing or ill-typed field
    to zero. `attempts` is 1 for a single envelope; callers sum across
    attempts via `combine`.

    Usage is captured regardless of `is_error` / `subtype`: an error
    envelope still cost tokens, so we record it (with `is_error` set).
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict):
        return None

    usage = envelope.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    return ReviewerUsage(
        agent_name=agent_name,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
        cost_usd=_float(envelope.get("total_cost_usd")),
        duration_ms=_int(envelope.get("duration_ms")),
        attempts=1,
        is_error=bool(envelope.get("is_error")),
    )


def combine(a: ReviewerUsage, b: ReviewerUsage) -> ReviewerUsage:
    """Sum two usages for the same reviewer (e.g. across retries).

    Token, cost, and duration fields add; `attempts` adds; `is_error` is
    True if either attempt errored. `agent_name` is taken from `a` (callers
    only ever combine usages for the same reviewer).
    """
    return ReviewerUsage(
        agent_name=a.agent_name,
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_creation_input_tokens=(
            a.cache_creation_input_tokens + b.cache_creation_input_tokens
        ),
        cache_read_input_tokens=(
            a.cache_read_input_tokens + b.cache_read_input_tokens
        ),
        cost_usd=a.cost_usd + b.cost_usd,
        duration_ms=a.duration_ms + b.duration_ms,
        attempts=a.attempts + b.attempts,
        is_error=a.is_error or b.is_error,
    )


def _int(value: object) -> int:
    # `bool` is an `int` subclass; exclude it so `"input_tokens": true`
    # becomes 0 rather than 1. Mirrors the bool-vs-int guard in
    # reviewer._finding_from_raw and config._config_from_dict.
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) else 0


def _float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


# --- per-run record (issue #19) --------------------------------------------

@dataclass(frozen=True)
class ReviewerRow:
    """One reviewer's contribution to a run record: its verdict plus the
    usage captured for it. Verdicts that never ran a claude -p call (missing
    persona, crash, the diff-size meta-WARN) carry zero usage."""
    name: str
    verdict: str
    findings: int
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    is_error: bool = False


@dataclass(frozen=True)
class RunRecord:
    """One push's metrics. Stores raw per-reviewer rows only — run-level
    totals and verdict counts are deliberately NOT stored; they are folds
    over `reviewers` computed by the Phase 3 stats reader."""
    schema_version: int
    ts: str
    model: str
    changed_lines: int
    exit_code: int
    reviewers: list[ReviewerRow] = field(default_factory=list)


def build_run_record(
    verdicts: list[Verdict],
    usages: list[ReviewerUsage],
    *,
    model: str,
    changed_lines: int,
    exit_code: int,
    timestamp: str | None = None,
) -> RunRecord:
    """Join verdicts with their captured usage (by agent_name) into one
    RunRecord. A verdict with no matching usage gets a zero-usage row so
    synthetic verdicts still appear without distorting cache ratios."""
    usage_by_name = {u.agent_name: u for u in usages}
    rows: list[ReviewerRow] = []
    for v in verdicts:
        u = usage_by_name.get(v.agent_name)
        rows.append(ReviewerRow(
            name=v.agent_name,
            verdict=v.verdict,
            findings=len(v.findings),
            attempts=u.attempts if u else 0,
            input_tokens=u.input_tokens if u else 0,
            output_tokens=u.output_tokens if u else 0,
            cache_creation_input_tokens=u.cache_creation_input_tokens if u else 0,
            cache_read_input_tokens=u.cache_read_input_tokens if u else 0,
            cost_usd=u.cost_usd if u else 0.0,
            duration_ms=u.duration_ms if u else 0,
            is_error=u.is_error if u else False,
        ))
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return RunRecord(
        schema_version=SCHEMA_VERSION,
        ts=ts,
        model=model,
        changed_lines=changed_lines,
        exit_code=exit_code,
        reviewers=rows,
    )


def format_summary(record: RunRecord) -> str:
    """One-line, ASCII-only per-run summary for stderr. ASCII so it can't
    trip a cp1252 stderr on Windows when the hook runs outside the
    UTF-8-reconfigured entry point."""
    rows = record.reviewers
    n = len(rows)
    input_t = sum(r.input_tokens for r in rows)
    output_t = sum(r.output_tokens for r in rows)
    cc = sum(r.cache_creation_input_tokens for r in rows)
    cr = sum(r.cache_read_input_tokens for r in rows)
    total = input_t + output_t + cc + cr
    cost = sum(r.cost_usd for r in rows)
    # Cache hit ratio is over cacheable *input* only (output isn't cached).
    cache_basis = input_t + cc + cr
    pct = round(100 * cr / cache_basis) if cache_basis else 0
    return (
        f"claude-multi-agent-review: {n} reviewer{'s' if n != 1 else ''}, "
        f"{_human(total)} tokens ({_human(cr)} cached, {pct}%), "
        f"${cost:.4f}"
    )


def record_run(
    config: Config,
    verdicts: list[Verdict],
    usages: list[ReviewerUsage],
    *,
    changed_lines: int,
    exit_code: int,
) -> str:
    """Build the run record, append it to the metrics log (best-effort),
    keep it out of git, and return the one-line summary for the caller to
    print. Caller is responsible for gating on `config.metrics_enabled`."""
    record = build_run_record(
        verdicts, usages,
        model=config.model,
        changed_lines=changed_lines,
        exit_code=exit_code,
    )
    summary = format_summary(record)

    target = _resolve_within_repo(config.repo_root, config.metrics_path)
    if target is None:
        # metrics_path escapes the repo root — refuse to write outside it
        # (mirrors the spec/persona path-traversal guards). The summary
        # still returns so the per-run line shows.
        print(
            f"claude-multi-agent-review: metrics_path "
            f"{config.metrics_path!r} resolves outside the repo root; "
            "skipping metrics write.",
            file=sys.stderr, flush=True,
        )
        return summary

    _ensure_gitignored(config.repo_root, config.metrics_path)
    _append_jsonl(target, record)
    return summary


# --- persistence helpers ----------------------------------------------------

def _append_jsonl(path: Path, record: RunRecord) -> None:
    line = json.dumps(asdict(record), separators=(",", ":"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        # Best-effort: metrics persistence must never break a review or
        # change the push outcome. A write failure degrades to a notice.
        print(
            f"claude-multi-agent-review: could not write metrics to "
            f"{path}: {e}",
            file=sys.stderr, flush=True,
        )


def _ensure_gitignored(repo_root: Path, rel_path: str) -> None:
    """Append `rel_path` to the repo's .gitignore if not already present.

    Idempotent and best-effort: metrics are on by default, so we keep the
    log out of `git status` for the user rather than surprising them with
    an untracked file. A failure here must not break the push.
    """
    gitignore = repo_root / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
        if rel_path in {ln.strip() for ln in existing.splitlines()}:
            return
        prefix = "" if existing == "" or existing.endswith("\n") else "\n"
        with gitignore.open("a", encoding="utf-8") as f:
            f.write(f"{prefix}{rel_path}\n")
    except OSError:
        # Best-effort (same rationale as _append_jsonl); worst case the
        # user sees the metrics file as untracked and can ignore it.
        return


def _resolve_within_repo(repo_root: Path, rel_path: str) -> Path | None:
    """Resolve `rel_path` under `repo_root`, returning None if it escapes
    (path-traversal safeguard, mirroring orchestrate._read_spec)."""
    root = repo_root.resolve()
    candidate = (repo_root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _human(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"
