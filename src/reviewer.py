"""Single-reviewer subprocess wrapper.

One call to `review()` = one persona's complete review lifecycle:
    build claude -p command -> run -> classify failures -> retry with the
    right strategy -> on exhaustion, synthesize a Verdict honoring
    config.treat_reviewer_failure_as.

This module never raises out of `review()` for failure cases; callers always
get a Verdict back. That property is what makes the orchestrator simple
— a single flaky persona cannot crash the push.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .aggregate import Finding, Verdict
from .config import Config

# Reviewers are read-only. This list is passed verbatim to --disallowedTools
# so the subagent cannot mutate the repo mid-review.
DISALLOWED_TOOLS = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,TodoWrite"

# Short instruction stub passed via -p. The persona system prompt carries the
# real semantic load; this just orients the model to where the payload is.
_INSTRUCTION = (
    "Review the spec and diff supplied on stdin against the criteria in your "
    "system prompt. Respond with a single JSON object matching the verdict "
    "schema described there. Do not include any prose outside the JSON."
)

FailureClass = Literal["transient", "parse", "auth", "environment"]
# Failure classes that are not retried (no amount of waiting will fix them).
_NON_RETRIABLE: frozenset[FailureClass] = frozenset({"auth", "environment"})


@dataclass(frozen=True)
class _AttemptOutcome:
    """Result of one claude -p attempt. Exactly one of verdict / failure
    is populated."""
    verdict: Verdict | None
    failure_class: FailureClass | None
    reason: str  # human-readable, for logs and synthetic-verdict reasoning


# --- public entry point -----------------------------------------------------

def review(
    *,
    persona_name: str,
    persona_path: Path,
    spec: str,
    diff_payload: str,
    config: Config,
    log: Callable[[str], None] | None = None,
) -> Verdict:
    """Run one reviewer end-to-end.

    `log`, when provided, receives single-line retry-progress messages.
    The caller is responsible for synchronizing the underlying writer if
    this reviewer is dispatched alongside other concurrent writers (the
    orchestrator passes a lock-aware logger per CLAUDE.md
    output.streaming-via-lock; callers that don't share a stream — e.g.
    scripts/smoke_review.py — can omit it or pass any plain printer).
    """
    persona_prompt = persona_path.read_text(encoding="utf-8")
    stdin_payload = _format_stdin_payload(spec=spec, diff_payload=diff_payload)

    max_attempts = config.reviewer_retries + 1
    append_system_prompt = ""
    last_outcome: _AttemptOutcome | None = None

    for attempt in range(1, max_attempts + 1):
        outcome = _run_one_attempt(
            persona_name=persona_name,
            persona_prompt=persona_prompt,
            append_system_prompt=append_system_prompt,
            stdin_payload=stdin_payload,
            config=config,
        )

        if outcome.verdict is not None:
            return outcome.verdict

        last_outcome = outcome
        if outcome.failure_class in _NON_RETRIABLE:
            break  # retrying these is pointless — no amount of waiting helps

        if attempt < max_attempts:
            if log is not None:
                log(
                    f"[{persona_name}] retry {attempt}/{max_attempts - 1}: "
                    f"{outcome.reason}"
                )
            if outcome.failure_class == "transient":
                time.sleep(2)
            elif outcome.failure_class == "parse":
                append_system_prompt = (
                    "Your previous response was not a valid JSON object "
                    "matching the verdict schema. Emit exactly one JSON "
                    "object and nothing else."
                )

    assert last_outcome is not None  # loop runs at least once
    actual_attempts = (
        attempt if last_outcome.failure_class in _NON_RETRIABLE else max_attempts
    )
    return _synthetic_verdict(
        persona_name=persona_name,
        outcome=last_outcome,
        attempts=actual_attempts,
        mode=config.treat_reviewer_failure_as,
    )


# --- one attempt ------------------------------------------------------------

def _run_one_attempt(
    *,
    persona_name: str,
    persona_prompt: str,
    append_system_prompt: str,
    stdin_payload: str,
    config: Config,
) -> _AttemptOutcome:
    cmd = [
        "claude",
        "-p", _INSTRUCTION,
        "--system-prompt", persona_prompt,
        "--output-format", "json",
        "--disallowedTools", DISALLOWED_TOOLS,
        "--model", config.model,
    ]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]

    try:
        proc = subprocess.run(
            cmd,
            input=stdin_payload,
            capture_output=True,
            text=True,
            # Force UTF-8 regardless of platform locale. Without this,
            # `text=True` uses locale.getpreferredencoding(False) — cp1252
            # on Windows — which can't encode characters like U+2192 (→)
            # that legitimately appear in spec text and persona prompts.
            encoding="utf-8",
            timeout=config.reviewer_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _AttemptOutcome(
            verdict=None,
            failure_class="transient",
            reason=f"claude -p timed out after {config.reviewer_timeout_seconds}s",
        )
    except FileNotFoundError:
        # `claude` binary not on PATH. Not retriable — user needs to install
        # or fix their PATH before another attempt would succeed.
        return _AttemptOutcome(
            verdict=None,
            failure_class="environment",
            reason="`claude` CLI not found on PATH (is Claude Code installed?)",
        )
    except UnicodeError as e:
        # Defense in depth: encoding="utf-8" above should make this
        # unreachable, but if some future code path produces bytes the
        # interpreter can't round-trip, return a synthetic verdict rather
        # than letting it raise out of review() (CLAUDE.md
        # errors.reviewer-never-raises-on-failure).
        #
        # Classified as `environment`, not `transient`: unicode errors on
        # specific bytes are deterministic — retrying the identical
        # subprocess call against the identical inputs will always
        # reproduce the same failure. `_NON_RETRIABLE` skips the retry
        # loop for this class, avoiding wasted `claude -p` calls.
        return _AttemptOutcome(
            verdict=None,
            failure_class="environment",
            reason=f"unicode error in subprocess I/O: {e}",
        )

    if proc.returncode != 0:
        failure_class = _classify_subprocess_failure(proc.returncode, proc.stderr)
        return _AttemptOutcome(
            verdict=None,
            failure_class=failure_class,
            reason=f"claude -p exited {proc.returncode}: {_trim(proc.stderr)}",
        )

    verdict_or_reason = _parse_verdict(proc.stdout, persona_name=persona_name)
    if isinstance(verdict_or_reason, Verdict):
        return _AttemptOutcome(verdict=verdict_or_reason, failure_class=None, reason="")
    return _AttemptOutcome(
        verdict=None,
        failure_class="parse",
        reason=verdict_or_reason,
    )


# --- failure classification -------------------------------------------------

_AUTH_PATTERNS = re.compile(
    # `\b` boundaries on most terms to avoid false positives; the api-key
    # variant deliberately omits word boundaries so "ANTHROPIC_API_KEY"
    # (single identifier, no boundary before "api") still matches —
    # caught by tests/test_reviewer.py.
    r"\b(401|403|unauthorized|authentication|forbidden)\b|api[_\s-]?key",
    re.IGNORECASE,
)


def _classify_subprocess_failure(returncode: int, stderr: str) -> FailureClass:
    if _AUTH_PATTERNS.search(stderr or ""):
        return "auth"
    return "transient"


# --- payload + parsing ------------------------------------------------------

def _format_stdin_payload(*, spec: str, diff_payload: str) -> str:
    """Stable prefix (spec) first, push-specific content (diff) second. This
    ordering matters: parallel reviewers reviewing the same push share the
    spec prefix, which gives prompt-cache hits across the fan-out."""
    return (
        "=== PROJECT SPEC (CLAUDE.md) ===\n"
        f"{spec.strip()}\n"
        "\n"
        "=== PUSH UNDER REVIEW ===\n"
        f"{diff_payload.strip()}\n"
    )


def _parse_verdict(stdout: str, *, persona_name: str) -> Verdict | str:
    """Two-stage parse. Returns a Verdict on success, or a reason string on
    failure (so the caller can wrap it in an _AttemptOutcome)."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        return f"claude -p output was not valid JSON: {e}"

    # Valid JSON includes non-objects (lists, strings, numbers, null).
    # `.get()` on a non-dict raises AttributeError, which would propagate
    # out of review() and violate errors.reviewer-never-raises-on-failure.
    if not isinstance(envelope, dict):
        return "claude -p output was JSON but not a JSON object"

    result = envelope.get("result")
    if not isinstance(result, str):
        return "claude -p envelope missing string `result` field"

    inner_text = result.strip()
    try:
        raw = json.loads(inner_text)
    except json.JSONDecodeError:
        extracted = _extract_first_json_object(inner_text)
        if extracted is None:
            return "reviewer response did not contain a JSON object"
        try:
            raw = json.loads(extracted)
        except json.JSONDecodeError as e:
            return f"reviewer response JSON was malformed: {e}"

    return _verdict_from_raw(raw, persona_name=persona_name)


def _verdict_from_raw(raw: object, *, persona_name: str) -> Verdict | str:
    if not isinstance(raw, dict):
        return "reviewer response was not a JSON object"

    required = ("agent_name", "verdict", "summary", "reasoning", "findings")
    missing = [k for k in required if k not in raw]
    if missing:
        return f"reviewer response missing required fields: {', '.join(missing)}"

    agent = raw["agent_name"]
    if agent != persona_name:
        return (
            f"reviewer response agent_name={agent!r} does not match "
            f"persona {persona_name!r}"
        )

    level = raw["verdict"]
    if level not in ("PASS", "WARN", "FAIL"):
        return f"reviewer response verdict={level!r} is not PASS/WARN/FAIL"

    summary = raw["summary"]
    reasoning = raw["reasoning"]
    if not isinstance(summary, str) or not isinstance(reasoning, str):
        return "reviewer response summary/reasoning must be strings"

    raw_findings = raw["findings"]
    if not isinstance(raw_findings, list):
        return "reviewer response findings must be a list"

    findings: list[Finding] = []
    for i, item in enumerate(raw_findings):
        finding_or_reason = _finding_from_raw(item, index=i)
        if isinstance(finding_or_reason, str):
            return finding_or_reason
        findings.append(finding_or_reason)

    return Verdict(
        agent_name=agent,
        verdict=level,
        summary=summary,
        reasoning=reasoning,
        findings=findings,
    )


def _finding_from_raw(item: object, *, index: int) -> Finding | str:
    if not isinstance(item, dict):
        return f"finding #{index} is not an object"

    severity = item.get("severity")
    if severity not in ("info", "warn", "error"):
        return f"finding #{index} severity={severity!r} is not info/warn/error"

    message = item.get("message")
    if not isinstance(message, str) or not message.strip():
        return f"finding #{index} missing non-empty message"

    file_ = item.get("file")
    line = item.get("line")

    # `bool` is a subclass of `int` in Python, so `isinstance(True, int)`
    # returns True. Reject bools explicitly so `"line": true` doesn't
    # silently pass validation. This matches config._config_from_dict's
    # bool-vs-int guard so the two validators are consistent.
    line_is_int = isinstance(line, int) and not isinstance(line, bool)

    if severity == "error":
        if not isinstance(file_, str) or not file_:
            return f"finding #{index} severity=error requires `file`"
        if not line_is_int:
            return f"finding #{index} severity=error requires integer `line`"

    if file_ is not None and not isinstance(file_, str):
        return f"finding #{index} `file` must be a string"
    if line is not None and not line_is_int:
        return f"finding #{index} `line` must be an integer"

    spec_rule = item.get("spec_rule")
    if spec_rule is not None and not isinstance(spec_rule, str):
        return f"finding #{index} `spec_rule` must be a string"

    return Finding(
        severity=severity,
        message=message,
        file=file_ if isinstance(file_, str) else None,
        line=line if line_is_int else None,
        spec_rule=spec_rule if isinstance(spec_rule, str) else None,
    )


def _extract_first_json_object(text: str) -> str | None:
    """Return the substring of the first balanced {...} block, or None.

    Used as a last-resort fallback when the model wraps the verdict JSON in
    prose despite instructions. Tracks string literals so braces inside
    strings don't fool the balance counter.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# --- synthetic verdict for exhausted failures -------------------------------

def _synthetic_verdict(
    *,
    persona_name: str,
    outcome: _AttemptOutcome,
    attempts: int,
    mode: str,
) -> Verdict:
    level: Literal["WARN", "FAIL"] = "FAIL" if mode == "fail" else "WARN"
    fc = outcome.failure_class or "unknown"
    return Verdict(
        agent_name=persona_name,
        verdict=level,
        summary=f"reviewer failed: {fc} error (after {attempts} attempt{'s' if attempts != 1 else ''})",
        reasoning=(
            f"The {persona_name} reviewer could not complete this review.\n"
            f"Failure class: {fc}\n"
            f"Last reason: {outcome.reason}\n"
            f"This is a synthetic verdict produced by claude-multi-agent-review "
            f"because treat_reviewer_failure_as = {mode!r}."
        ),
        findings=[],
    )


# --- helpers ----------------------------------------------------------------

def _trim(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "... [truncated]"
