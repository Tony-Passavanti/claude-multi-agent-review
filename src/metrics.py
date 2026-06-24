"""Per-reviewer token/cost usage capture.

Parses token counts and actual cost out of the `claude -p --output-format
json` envelope so the hook can report tokens-per-run, cost-per-run, and
cache effectiveness over time. This module holds only the data shape and
the pure extraction/aggregation logic; persistence (issue #19) and
cumulative reporting (issue #20) live in later phases.

Two deliberate properties:

  - Nothing here raises on malformed input. A missing or wrong-typed
    `usage` block yields a zero-valued ReviewerUsage (or None when there
    is no envelope at all). `reviewer.review()` depends on this to honor
    CLAUDE.md errors.reviewer-never-raises-on-failure.

  - Token counts are kept as a four-way split and never pre-summed, so
    cache effectiveness — cache_read / (cache_read + cache_creation +
    input) — can be computed downstream. `cost_usd` is the CLI's
    authoritative `total_cost_usd`, not an estimate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


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
