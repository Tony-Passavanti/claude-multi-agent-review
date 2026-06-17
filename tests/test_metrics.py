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

from src.metrics import ReviewerUsage, combine, usage_from_stdout


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
