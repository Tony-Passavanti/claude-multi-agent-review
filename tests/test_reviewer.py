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
    # Guards the (?:\b|_) leading-boundary alternative. Without the
    # boundary prefix, these would mis-classify as auth.
    assert _classify_subprocess_failure(1, stderr) == "transient"


@pytest.mark.parametrize("stderr", [
    # Suffix continuations of "api_key" — regex must NOT match if
    # there's another word character after `key`. Guards the trailing
    # \b that protects against a too-permissive variant.
    "api_keys",
    "api_keyboard",
    "Loading api_keys from vault failed with 500",
    "Restarted api_key_rotation service",
    "apikeychain initialized",
])
def test_classify_does_not_match_api_key_suffix_extensions(stderr: str) -> None:
    # Without the trailing \b in (?:\b|_)api[_\s-]?key\b, log lines
    # like "Loading api_keys from vault failed with 500" would
    # mis-classify as auth instead of transient.
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


# --- review() persona-file read guard (closes issue #7) -------------------
#
# CLAUDE.md errors.reviewer-never-raises-on-failure: review() MUST always
# return a Verdict, even when the persona file is unreadable. Previously
# this raised OSError out of review() because the read_text call had no
# guard. The orchestrator's broad except still caught it, but the contract
# on review() itself was technically broken.

from pathlib import Path  # noqa: E402 — kept local to the new section

from src.config import Config  # noqa: E402
from src.reviewer import review  # noqa: E402


def _basic_config(install_root: Path, mode: str = "warn") -> Config:
    return Config(
        spec_path=Path("CLAUDE.md"),
        default_branch="",
        enabled_personas=["x"],
        model="claude-sonnet-4-6",
        parallel=False,
        review_tags=False,
        override_env="CLAUDE_MULTI_AGENT_REVIEW_OVERRIDE",
        reviewer_timeout_seconds=180,
        reviewer_retries=1,
        treat_reviewer_failure_as=mode,
        max_diff_lines=5000,
        install_root=install_root,
        repo_root=install_root,
    )


def test_review_with_nonexistent_persona_path_returns_synthetic_verdict(
    tmp_path: Path,
) -> None:
    cfg = _basic_config(tmp_path)
    missing = tmp_path / "definitely-does-not-exist.md"
    v = review(
        persona_name="ghost",
        persona_path=missing,
        spec="# spec",
        diff_payload="diff",
        config=cfg,
    )
    assert isinstance(v, Verdict)
    assert v.agent_name == "ghost"
    assert v.verdict == "WARN"
    # The path or its name appears in the reasoning for diagnosability.
    # NOTE: deliberately NOT asserting on v.summary's content — the
    # classification visibility is checked in
    # test_review_persona_read_failure_classified_environment via the
    # reasoning field, which is the most reliable surface (the summary's
    # format is _synthetic_verdict's responsibility and may change).
    assert "definitely-does-not-exist.md" in v.reasoning


def test_review_persona_read_failure_fail_mode(tmp_path: Path) -> None:
    # When treat_reviewer_failure_as="fail", the synthetic verdict is
    # FAIL — push gets blocked — instead of WARN.
    cfg = _basic_config(tmp_path, mode="fail")
    v = review(
        persona_name="ghost",
        persona_path=tmp_path / "missing.md",
        spec="# spec",
        diff_payload="diff",
        config=cfg,
    )
    assert v.verdict == "FAIL"


def test_review_persona_read_failure_does_not_invoke_subprocess(
    tmp_path: Path, monkeypatch,
) -> None:
    # If read_text fails, we never get to claude -p. Verify that by
    # monkeypatching subprocess.run to fail loudly if called.
    import subprocess as subprocess_mod
    called = []
    def loud_run(*a, **kw):
        called.append((a, kw))
        raise AssertionError("subprocess.run must not be invoked when "
                             "persona file is unreadable")
    monkeypatch.setattr(subprocess_mod, "run", loud_run)
    cfg = _basic_config(tmp_path)
    v = review(
        persona_name="ghost",
        persona_path=tmp_path / "missing.md",
        spec="# spec",
        diff_payload="diff",
        config=cfg,
    )
    assert v.verdict == "WARN"
    assert called == []


def test_review_persona_read_failure_classified_environment(
    tmp_path: Path,
) -> None:
    # The synthetic verdict's reasoning must name the failure class
    # as `environment` (not transient, auth, or parse) — that's what
    # tells the operator the file isn't going to appear and a retry
    # won't help.
    cfg = _basic_config(tmp_path)
    v = review(
        persona_name="ghost",
        persona_path=tmp_path / "missing.md",
        spec="# spec",
        diff_payload="diff",
        config=cfg,
    )
    assert "environment" in v.reasoning


def test_review_with_unreadable_persona_path_returns_synthetic_verdict(
    tmp_path: Path, monkeypatch,
) -> None:
    # Distinct from "file missing": file exists but read_text raises
    # PermissionError (e.g., restrictive ACLs). Cross-platform-safe via
    # monkeypatch rather than chmod.
    persona_path = tmp_path / "restricted.md"
    persona_path.write_text("# whatever\n", encoding="utf-8")

    def deny_read(self, *a, **kw):  # noqa: ANN001
        raise PermissionError(f"Permission denied: {self}")

    monkeypatch.setattr(Path, "read_text", deny_read)

    cfg = _basic_config(tmp_path)
    v = review(
        persona_name="locked",
        persona_path=persona_path,
        spec="# spec",
        diff_payload="diff",
        config=cfg,
    )
    assert v.verdict == "WARN"
    assert "Permission denied" in v.reasoning


def test_review_with_non_utf8_persona_file_returns_synthetic_verdict(
    tmp_path: Path,
) -> None:
    # Persona file exists and is readable but contains non-UTF-8 bytes
    # (e.g. a Windows-1252 override or a file cloned with the wrong
    # encoding). `Path.read_text(encoding="utf-8")` raises
    # `UnicodeDecodeError`, which inherits from `ValueError`, NOT
    # `OSError`. The guard must catch `UnicodeError` (the parent of
    # UnicodeDecodeError) explicitly or this case escapes review()
    # and violates errors.reviewer-never-raises-on-failure.
    persona_path = tmp_path / "bad-encoding.md"
    # 0xe9 is `é` in Windows-1252 / Latin-1 but is an invalid start
    # byte for a continuation in UTF-8.
    persona_path.write_bytes(b"# persona\nthis byte is bad: \xe9\n")
    cfg = _basic_config(tmp_path)
    v = review(
        persona_name="badencoding",
        persona_path=persona_path,
        spec="# spec",
        diff_payload="diff",
        config=cfg,
    )
    assert isinstance(v, Verdict)
    assert v.verdict == "WARN"
    # The file name appears in the reasoning so a user troubleshooting
    # this WARN can find the offending persona file. Deliberately NOT
    # asserting on prefix phrasing (e.g. "could not read") — that's
    # `_synthetic_verdict`'s reason-template detail, not a contract
    # of `review()` itself. The environment-classification property
    # is covered by test_review_persona_read_failure_classified_environment.
    assert "bad-encoding.md" in v.reasoning
