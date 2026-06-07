"""Verdict schema + aggregation logic.

The schema is intentionally narrow so persona prompts can be precise about
what they must emit. The aggregator turns N verdicts into one exit code and
one formatted report.

This is the minimal aggregator: it implements the exit-code rules and
produces a scannable plaintext report. Polish (color, clickable file:line
paths, grouping by file) is tracked at issue #1. The public signature
`aggregate(verdicts) -> (exit_code, report)` is stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["info", "warn", "error"]
VerdictLevel = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True)
class Finding:
    severity: Severity
    message: str
    file: str | None = None
    line: int | None = None
    spec_rule: str | None = None


@dataclass(frozen=True)
class Verdict:
    agent_name: str
    verdict: VerdictLevel
    summary: str
    reasoning: str
    findings: list[Finding] = field(default_factory=list)


def aggregate(verdicts: list[Verdict]) -> tuple[int, str]:
    """Combine reviewer verdicts into (exit_code, formatted_report).

    Exit codes:
        0 - push allowed (all PASS, or WARN with no FAIL)
        1 - push blocked (at least one FAIL)
    """
    if not verdicts:
        return 0, "claude-multi-agent-review: no verdicts (nothing to review)\n"

    has_fail = any(v.verdict == "FAIL" for v in verdicts)
    has_warn = any(v.verdict == "WARN" for v in verdicts)

    lines: list[str] = []
    lines.append("")
    lines.append("=" * 64)
    lines.append("claude-multi-agent-review")
    lines.append("=" * 64)

    # Order verdicts: FAIL first, then WARN, then PASS. Within each level,
    # preserve completion order (which is the input order). Makes the
    # important stuff readable without scrolling.
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    ordered = sorted(enumerate(verdicts), key=lambda iv: (order[iv[1].verdict], iv[0]))

    for _, v in ordered:
        lines.append("")
        lines.append(f"[{v.agent_name}] {v.verdict:<4}  {v.summary}")
        for f in v.findings:
            loc = f"{f.file}:{f.line}" if f.file else "(no location)"
            rule = f" [{f.spec_rule}]" if f.spec_rule else ""
            lines.append(f"  - {f.severity:<5} {loc}{rule}")
            lines.append(f"      {f.message}")
        # Only print reasoning for non-PASS verdicts: PASS-with-reasoning
        # is noise; the summary is enough.
        if v.verdict != "PASS" and v.reasoning.strip():
            lines.append("    reasoning:")
            for r_line in v.reasoning.strip().splitlines():
                lines.append(f"      {r_line}")

    lines.append("")
    lines.append("=" * 64)
    if has_fail:
        lines.append("PUSH BLOCKED: at least one reviewer returned FAIL.")
        lines.append("Bypass: set CLAUDE_MULTI_AGENT_REVIEW_OVERRIDE=1, or `git push --no-verify`")
        lines.append("=" * 64)
        return 1, "\n".join(lines) + "\n"
    if has_warn:
        lines.append("PUSH ALLOWED with warnings.")
        lines.append("=" * 64)
        return 0, "\n".join(lines) + "\n"
    lines.append("PUSH ALLOWED: all reviewers PASS.")
    lines.append("=" * 64)
    return 0, "\n".join(lines) + "\n"
