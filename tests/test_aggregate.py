"""Tests for src/aggregate.py.

Covers the exit-code rules that determine whether a push is blocked, the
verdict-ordering logic in the report, and the print_report() helper that
satisfies CLAUDE.md output.stdout-reserved-for-aggregator.
"""

from __future__ import annotations

import pytest

from src.aggregate import Finding, Verdict, aggregate, print_report


def _v(name: str, level: str, *findings: Finding, reasoning: str = "") -> Verdict:
    """Compact constructor for tests."""
    return Verdict(
        agent_name=name,
        verdict=level,
        summary=f"{name} {level} summary",
        reasoning=reasoning or f"{name} reasoning",
        findings=list(findings),
    )


# --- exit-code rules -------------------------------------------------------

def test_empty_verdicts_allows_push() -> None:
    code, report = aggregate([])
    assert code == 0
    assert "no verdicts" in report


def test_all_pass_allows_push() -> None:
    code, report = aggregate([_v("a", "PASS"), _v("b", "PASS")])
    assert code == 0
    assert "PUSH ALLOWED: all reviewers PASS" in report


def test_warn_with_no_fail_allows_push() -> None:
    code, report = aggregate([_v("a", "WARN"), _v("b", "PASS")])
    assert code == 0
    assert "PUSH ALLOWED with warnings" in report


def test_any_fail_blocks_push() -> None:
    code, report = aggregate([_v("a", "PASS"), _v("b", "FAIL"), _v("c", "WARN")])
    assert code == 1
    assert "PUSH BLOCKED" in report


def test_fail_blocks_even_with_pass_majority() -> None:
    # Six PASS + one FAIL must still block.
    verdicts = [_v(f"p{i}", "PASS") for i in range(6)] + [_v("f", "FAIL")]
    code, _ = aggregate(verdicts)
    assert code == 1


# --- verdict ordering ------------------------------------------------------

def test_verdicts_ordered_fail_warn_pass() -> None:
    # Input order is PASS, FAIL, WARN. Output must list FAIL first,
    # then WARN, then PASS.
    code, report = aggregate([
        _v("alpha", "PASS"),
        _v("bravo", "FAIL"),
        _v("charlie", "WARN"),
    ])
    assert code == 1
    fail_pos = report.index("[bravo]")
    warn_pos = report.index("[charlie]")
    pass_pos = report.index("[alpha]")
    assert fail_pos < warn_pos < pass_pos


def test_input_order_preserved_within_same_level() -> None:
    # Two WARNs: input order is z then a. Output should preserve that.
    _, report = aggregate([_v("zulu", "WARN"), _v("alpha", "WARN")])
    z_pos = report.index("[zulu]")
    a_pos = report.index("[alpha]")
    assert z_pos < a_pos


# --- finding formatting ----------------------------------------------------

def test_finding_with_location_shows_file_line() -> None:
    finding = Finding(severity="error", message="leak", file="src/x.py", line=42)
    _, report = aggregate([_v("sec", "FAIL", finding)])
    assert "src/x.py:42" in report
    assert "leak" in report


def test_finding_without_location_shows_placeholder() -> None:
    finding = Finding(severity="info", message="note", file=None, line=None)
    _, report = aggregate([_v("a", "WARN", finding)])
    assert "(no location)" in report


def test_finding_with_spec_rule_shows_rule_id() -> None:
    finding = Finding(
        severity="warn",
        message="m",
        file="f.py",
        line=1,
        spec_rule="some.rule-id",
    )
    _, report = aggregate([_v("a", "WARN", finding)])
    assert "[some.rule-id]" in report


def test_finding_severity_displayed() -> None:
    findings = [
        Finding(severity="info", message="i", file=None, line=None),
        Finding(severity="warn", message="w", file=None, line=None),
        Finding(severity="error", message="e", file="f.py", line=1),
    ]
    _, report = aggregate([_v("multi", "FAIL", *findings)])
    assert "info" in report
    assert "warn" in report
    assert "error" in report


# --- reasoning display rules -----------------------------------------------

def test_reasoning_shown_for_fail() -> None:
    _, report = aggregate([_v("x", "FAIL", reasoning="why-it-fails")])
    assert "why-it-fails" in report


def test_reasoning_shown_for_warn() -> None:
    _, report = aggregate([_v("x", "WARN", reasoning="why-it-warns")])
    assert "why-it-warns" in report


def test_reasoning_hidden_for_pass() -> None:
    # PASS-with-reasoning would be noise: the summary line is enough.
    _, report = aggregate([_v("x", "PASS", reasoning="long-pass-reasoning")])
    assert "long-pass-reasoning" not in report


# --- print_report ----------------------------------------------------------

def test_print_report_writes_to_stdout(capsys) -> None:
    print_report("hello world")
    captured = capsys.readouterr()
    assert "hello world" in captured.out
    assert captured.err == ""


def test_print_report_writes_only_to_stdout(capsys) -> None:
    # Spec rule output.stdout-reserved-for-aggregator: the aggregator's
    # report goes to stdout, nothing else.
    print_report("PUSH ALLOWED: all reviewers PASS")
    captured = capsys.readouterr()
    assert captured.err == ""
