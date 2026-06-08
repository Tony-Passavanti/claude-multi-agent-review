"""Smoke test: exercise reviewer.review() against a real claude -p call.

Used to validate the subprocess wrapper outside the full hook flow. Not
part of the test suite — a dev tool.

The fixture spec defines one explicit FAIL-level rule; the fixture diff
violates it. A correctly-functioning reviewer should return verdict=FAIL
with at least one error-severity finding citing the rule.

Run:
    python scripts/smoke_review.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import reviewer  # noqa: E402
from src.config import Config  # noqa: E402


SPEC = """\
# Project Spec (synthetic, smoke-test fixture)

## Logging
- All output from code under `src/` MUST go through the project logger
  (`from src.log import logger`).
- Direct `print()` calls in `src/` are PROHIBITED. This is a hard rule:
  any violation is a FAIL-level finding. Rule id: `logging.no-print-in-src`.

## Style
- New public functions SHOULD have type hints. Missing hints are a WARN,
  not a FAIL. Rule id: `style.type-hints-on-public`.
"""

DIFF = """\
=== ref: refs/heads/feature/hello (1 commit, base: origin/main@abc1234) ===

abc1234 Add hello function

diff --git a/src/hello.py b/src/hello.py
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/src/hello.py
@@ -0,0 +1,5 @@
+def hello(name: str) -> None:
+    print(f"Hello, {name}!")
+
+def main() -> None:
+    hello("world")
"""


def main() -> int:
    config = Config(
        spec_path=Path("CLAUDE.md"),
        default_branch="",
        enabled_personas=["spec_conformance"],
        model="claude-sonnet-4-6",
        parallel=True,
        review_tags=False,
        override_env="CLAUDE_MULTI_AGENT_REVIEW_OVERRIDE",
        reviewer_timeout_seconds=180,
        reviewer_retries=1,
        treat_reviewer_failure_as="warn",
        max_diff_lines=5000,
        install_root=ROOT,
        repo_root=ROOT,
    )

    persona_path = ROOT / "src" / "personas" / "spec_conformance.md"

    print(f"persona:  {persona_path.relative_to(ROOT)}", file=sys.stderr)
    print(f"model:    {config.model}", file=sys.stderr)
    print(f"spec:     {len(SPEC)} chars (synthetic)", file=sys.stderr)
    print(f"diff:     {len(DIFF)} chars (synthetic, contains 1 rule violation)", file=sys.stderr)
    print("running claude -p ...", file=sys.stderr)
    print("", file=sys.stderr)

    verdict = reviewer.review(
        persona_name="spec_conformance",
        persona_path=persona_path,
        spec=SPEC,
        diff_payload=DIFF,
        config=config,
    )

    print("=" * 60)
    print(f"verdict:  {verdict.verdict}")
    print(f"summary:  {verdict.summary}")
    print(f"findings: {len(verdict.findings)}")
    for f in verdict.findings:
        loc = f"{f.file}:{f.line}" if f.file else "(no location)"
        rule = f" [{f.spec_rule}]" if f.spec_rule else ""
        print(f"  [{f.severity}] {loc}{rule}")
        print(f"    {f.message}")
    print("")
    print("reasoning:")
    print(verdict.reasoning)
    print("=" * 60)

    expected = "FAIL"
    if verdict.verdict == expected:
        print(f"\nsmoke PASSED: got {verdict.verdict} as expected", file=sys.stderr)
        return 0
    print(
        f"\nsmoke UNEXPECTED: got {verdict.verdict}, expected {expected}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
