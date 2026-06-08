"""Tests for src/reviewer.py.

Covers the verdict-parsing pipeline (`_parse_verdict`,
`_verdict_from_raw`, `_finding_from_raw`), the brace-tracking JSON
extractor (`_extract_first_json_object`), the failure classifier
(`_classify_subprocess_failure`), and the stdin payload formatter
(`_format_stdin_payload`).

These are the most safety-critical pure functions in the project:
the schema validators are what stand between an untrusted model
response and the rest of the system, and the JSON extractor is
the last-resort fallback when a model wraps its output in prose.
"""

from __future__ import annotations

import json

import pytest

from src.aggregate import Verdict
from src.reviewer import (
    _classify_subprocess_failure,
    _extract_first_json_object,
    _finding_from_raw,
    _format_stdin_payload,
    _parse_verdict,
    _verdict_from_raw,
)


# --- _extract_first_json_object --------------------------------------------

def test_extract_plain_json_object() -> None:
    assert _extract_first_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_wrapped_in_prose() -> None:
    text = 'Here is the result: {"a": 1, "b": 2} thanks for reading'
    extracted = _extract_first_json_object(text)
    assert extracted == '{"a": 1, "b": 2}'


def test_extract_handles_nested_objects() -> None:
    text = '  {"outer": {"inner": {"deep": 1}}, "side": 2}  '
    extracted = _extract_first_json_object(text)
    assert extracted == '{"outer": {"inner": {"deep": 1}}, "side": 2}'


def test_extract_ignores_braces_in_strings() -> None:
    # The string `"}}"` inside the JSON must not confuse the
    # balance counter into closing early.
    text = '{"key": "}}"} after'
    extracted = _extract_first_json_object(text)
    assert extracted == '{"key": "}}"}'


def test_extract_handles_escaped_quotes_in_strings() -> None:
    # `"He said \"hi\""` should not be interpreted as multiple strings.
    text = r'{"msg": "He said \"hi\"", "n": 1}'
    extracted = _extract_first_json_object(text)
    assert extracted == r'{"msg": "He said \"hi\"", "n": 1}'


def test_extract_returns_none_when_no_object() -> None:
    assert _extract_first_json_object("no braces anywhere here") is None
    assert _extract_first_json_object("") is None
    assert _extract_first_json_object("just [array]") is None


def test_extract_returns_first_of_multiple_objects() -> None:
    text = '{"first": 1} then {"second": 2}'
    extracted = _extract_first_json_object(text)
    assert extracted == '{"first": 1}'


# --- _classify_subprocess_failure ------------------------------------------

@pytest.mark.parametrize("stderr", [
    "Error: 401 Unauthorized",
    "HTTP 403 Forbidden",
    "Authentication failed",
    "Invalid API key",
    "Please set ANTHROPIC_API_KEY",
    "request unauthorized",
])
def test_classify_auth_patterns(stderr: str) -> None:
    assert _classify_subprocess_failure(1, stderr) == "auth"


@pytest.mark.parametrize("stderr", [
    "Connection refused",
    "Connection reset by peer",
    "Timeout exceeded",
    "Server returned 500",
    "HTTP 429 too many requests",  # rate-limit is transient, not auth
    "",
    "weird unexpected output",
])
def test_classify_transient_patterns(stderr: str) -> None:
    assert _classify_subprocess_failure(1, stderr) == "transient"


@pytest.mark.parametrize("stderr", [
    # Embedded substrings that LOOK like "api_key" but aren't —
    # regex must not classify these as auth.
    "rapidapikey",
    "POST /v1/rapidapikey/refresh returned 500",
    "decoded happikeyword",
])
def test_classify_does_not_match_embedded_api_key_substrings(stderr: str) -> None:
    # Guards the (?:\b|_) prefix that tightened the regex after a
    # too-permissive variant was caught in PR-A review. Without the
    # boundary prefix, these would mis-classify as auth.
    assert _classify_subprocess_failure(1, stderr) == "transient"


def test_classify_default_on_empty_stderr() -> None:
    # Defensive: empty stderr defaults to transient (cheap to retry)
    # rather than auth (which would silently degrade for non-auth
    # errors that happen to emit no stderr).
    assert _classify_subprocess_failure(1, "") == "transient"


# --- _format_stdin_payload -------------------------------------------------

def test_format_stdin_payload_includes_spec_header() -> None:
    payload = _format_stdin_payload(spec="# my spec", diff_payload="diff content")
    assert "=== PROJECT SPEC (CLAUDE.md) ===" in payload


def test_format_stdin_payload_includes_diff_header() -> None:
    payload = _format_stdin_payload(spec="s", diff_payload="d")
    assert "=== PUSH UNDER REVIEW ===" in payload


def test_format_stdin_payload_spec_precedes_diff() -> None:
    # Stable prefix (spec) MUST come first so parallel reviewers
    # reviewing the same push share the spec prefix in the prompt
    # cache. Diff is unique per push and comes second.
    payload = _format_stdin_payload(spec="SPEC_TEXT", diff_payload="DIFF_TEXT")
    spec_pos = payload.index("SPEC_TEXT")
    diff_pos = payload.index("DIFF_TEXT")
    assert spec_pos < diff_pos


def test_format_stdin_payload_strips_surrounding_whitespace() -> None:
    payload = _format_stdin_payload(spec="   spec   ", diff_payload="\n\ndiff\n\n")
    assert "spec" in payload
    assert "diff" in payload
    # Production strips whitespace from spec/diff before composing the
    # output. The verification must NOT itself call .strip() on the
    # section, or the test would pass whether or not stripping occurs.
    # Instead, assert that the section's content line (after the
    # header line and its newline) begins exactly with "spec" with no
    # whitespace ahead of the text.
    spec_section = payload.split("=== PROJECT SPEC (CLAUDE.md) ===\n")[1]
    # First character of the section payload must be the 's' of 'spec',
    # not a space — if production stops stripping, this assertion fails.
    assert spec_section.startswith("spec"), (
        f"expected stripped spec to start with 'spec', got {spec_section[:20]!r}"
    )


# --- _verdict_from_raw -----------------------------------------------------

def _good_verdict_dict(name: str = "architecture") -> dict:
    return {
        "agent_name": name,
        "verdict": "PASS",
        "summary": "ok",
        "reasoning": "all checks passed",
        "findings": [],
    }


def test_verdict_from_raw_happy_path() -> None:
    v = _verdict_from_raw(_good_verdict_dict(), persona_name="architecture")
    assert isinstance(v, Verdict)
    assert v.verdict == "PASS"
    assert v.findings == []


def test_verdict_from_raw_non_dict_input() -> None:
    result = _verdict_from_raw([1, 2, 3], persona_name="x")
    assert isinstance(result, str)
    assert "not a JSON object" in result


def test_verdict_from_raw_missing_required_fields() -> None:
    bad = {"agent_name": "x", "verdict": "PASS"}  # missing summary, reasoning, findings
    result = _verdict_from_raw(bad, persona_name="x")
    assert isinstance(result, str)
    assert "missing required fields" in result


def test_verdict_from_raw_wrong_agent_name() -> None:
    # Prompt-injection guard: agent_name must match the persona we
    # invoked. A response claiming to be a different reviewer is
    # rejected.
    bad = _good_verdict_dict(name="security")
    result = _verdict_from_raw(bad, persona_name="architecture")
    assert isinstance(result, str)
    assert "agent_name" in result
    assert "security" in result


@pytest.mark.parametrize("level", ["pass", "Pass", "FAILED", "ok", "OK", ""])
def test_verdict_from_raw_invalid_verdict_level(level: str) -> None:
    bad = _good_verdict_dict()
    bad["verdict"] = level
    result = _verdict_from_raw(bad, persona_name="architecture")
    assert isinstance(result, str)
    assert "verdict" in result


@pytest.mark.parametrize("level", ["PASS", "WARN", "FAIL"])
def test_verdict_from_raw_valid_verdict_levels(level: str) -> None:
    good = _good_verdict_dict()
    good["verdict"] = level
    v = _verdict_from_raw(good, persona_name="architecture")
    assert isinstance(v, Verdict)
    assert v.verdict == level


def test_verdict_from_raw_non_string_summary_rejected() -> None:
    bad = _good_verdict_dict()
    bad["summary"] = 42
    result = _verdict_from_raw(bad, persona_name="architecture")
    assert isinstance(result, str)


def test_verdict_from_raw_findings_not_a_list() -> None:
    bad = _good_verdict_dict()
    bad["findings"] = {"not": "a list"}
    result = _verdict_from_raw(bad, persona_name="architecture")
    assert isinstance(result, str)
    assert "findings" in result


# --- _finding_from_raw -----------------------------------------------------

def test_finding_from_raw_happy_path_error_severity() -> None:
    item = {
        "severity": "error",
        "message": "secret committed",
        "file": "src/cfg.py",
        "line": 42,
        "spec_rule": "security.no-secrets",
    }
    f = _finding_from_raw(item, index=0)
    assert hasattr(f, "severity")
    assert f.severity == "error"
    assert f.file == "src/cfg.py"
    assert f.line == 42


def test_finding_from_raw_happy_path_info_severity_no_location() -> None:
    item = {"severity": "info", "message": "fyi"}
    f = _finding_from_raw(item, index=0)
    assert hasattr(f, "severity")
    assert f.file is None
    assert f.line is None


def test_finding_from_raw_non_dict_rejected() -> None:
    result = _finding_from_raw("not a dict", index=0)
    assert isinstance(result, str)


@pytest.mark.parametrize("severity", ["INFO", "Warn", "fatal", "", "critical"])
def test_finding_from_raw_invalid_severity(severity: str) -> None:
    item = {"severity": severity, "message": "m"}
    result = _finding_from_raw(item, index=0)
    assert isinstance(result, str)


def test_finding_from_raw_missing_message() -> None:
    item = {"severity": "warn"}
    result = _finding_from_raw(item, index=0)
    assert isinstance(result, str)
    assert "message" in result


def test_finding_from_raw_empty_message() -> None:
    item = {"severity": "warn", "message": "  "}
    result = _finding_from_raw(item, index=0)
    assert isinstance(result, str)


def test_finding_from_raw_error_requires_file() -> None:
    item = {"severity": "error", "message": "m", "line": 10}  # no file
    result = _finding_from_raw(item, index=0)
    assert isinstance(result, str)
    assert "file" in result


def test_finding_from_raw_error_requires_line() -> None:
    item = {"severity": "error", "message": "m", "file": "x.py"}  # no line
    result = _finding_from_raw(item, index=0)
    assert isinstance(result, str)
    assert "line" in result


def test_finding_from_raw_bool_line_rejected() -> None:
    # bool subclasses int — without an explicit guard, `"line": true`
    # would silently pass validation. Same bool-vs-int trap as config.py.
    item = {"severity": "error", "message": "m", "file": "x.py", "line": True}
    result = _finding_from_raw(item, index=0)
    assert isinstance(result, str)
    assert "integer" in result


def test_finding_from_raw_string_line_rejected() -> None:
    item = {"severity": "error", "message": "m", "file": "x.py", "line": "42"}
    result = _finding_from_raw(item, index=0)
    assert isinstance(result, str)


def test_finding_from_raw_optional_spec_rule_must_be_string() -> None:
    item = {
        "severity": "warn",
        "message": "m",
        "spec_rule": 42,  # not a string
    }
    result = _finding_from_raw(item, index=0)
    assert isinstance(result, str)
    assert "spec_rule" in result


def test_finding_from_raw_index_appears_in_error_messages() -> None:
    result = _finding_from_raw("not a dict", index=3)
    assert isinstance(result, str)
    assert "#3" in result


# --- _parse_verdict --------------------------------------------------------

def _envelope(result_value: object) -> str:
    """Build the outer claude -p JSON envelope around a given result."""
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "result": result_value if isinstance(result_value, str) else json.dumps(result_value),
    })


def test_parse_verdict_happy_path() -> None:
    envelope = _envelope(_good_verdict_dict())
    v = _parse_verdict(envelope, persona_name="architecture")
    assert isinstance(v, Verdict)


def test_parse_verdict_envelope_not_json() -> None:
    result = _parse_verdict("not json", persona_name="x")
    assert isinstance(result, str)
    assert "not valid JSON" in result


def test_parse_verdict_envelope_is_list_not_object() -> None:
    # Real bug we fixed: valid JSON includes lists, strings, numbers,
    # null — `.get()` on those raises AttributeError. Must be rejected
    # with a clean parse-failure reason instead.
    envelope = json.dumps([1, 2, 3])
    result = _parse_verdict(envelope, persona_name="x")
    assert isinstance(result, str)
    assert "not a JSON object" in result


def test_parse_verdict_envelope_is_string_not_object() -> None:
    envelope = json.dumps("just a string")
    result = _parse_verdict(envelope, persona_name="x")
    assert isinstance(result, str)
    assert "not a JSON object" in result


def test_parse_verdict_envelope_is_null() -> None:
    envelope = json.dumps(None)
    result = _parse_verdict(envelope, persona_name="x")
    assert isinstance(result, str)


def test_parse_verdict_envelope_missing_result_field() -> None:
    envelope = json.dumps({"type": "result", "subtype": "success"})  # no `result`
    result = _parse_verdict(envelope, persona_name="x")
    assert isinstance(result, str)
    assert "result" in result


def test_parse_verdict_result_field_not_string() -> None:
    envelope = json.dumps({"result": 42})
    result = _parse_verdict(envelope, persona_name="x")
    assert isinstance(result, str)


def test_parse_verdict_result_json_invalid() -> None:
    envelope = json.dumps({"result": "this is just prose with no json"})
    result = _parse_verdict(envelope, persona_name="x")
    assert isinstance(result, str)


def test_parse_verdict_result_with_prose_wrapped_json() -> None:
    # Last-resort fallback: model wraps the verdict in prose.
    inner = json.dumps(_good_verdict_dict())
    wrapped = f"Here is my analysis: {inner} -- end."
    envelope = json.dumps({"result": wrapped})
    v = _parse_verdict(envelope, persona_name="architecture")
    assert isinstance(v, Verdict)
