# claude-multi-agent-review

A pre-push git hook that runs multiple Claude reviewers, in parallel,
against the code you're about to publish. Each reviewer reads your
project's `CLAUDE.md` spec and checks the diff through one specific
lens: architecture, security, tests, correctness, performance,
spec-conformance, or agent-authored failure modes.

The hook fires at `git push`, not `git commit`. It's a quality
checkpoint between agentic dev workflows and the shared remote — the
place where work from Codex, Claude Code, Cursor, and human authors
all converges before publication. It blocks pushes that violate the
spec your team agreed to, and gets out of the way otherwise.

> Status — initial implementation is live and reviews itself on every
> push. README and docs are still being filled in. Tests, polished
> install, and CI are tracked at the [open issues][issues]. The hook
> is usable today; the surrounding scaffolding is mid-flight.

[issues]: https://github.com/Tony-Passavanti/claude-multi-agent-review/issues

## What it looks like

When you push, the hook runs all configured reviewers in parallel and
streams verdicts as each one finishes:

```
$ git push origin feature/widget
claude-multi-agent-review: reviewing 1 ref, 184 changed lines with 7 reviewers (parallel): spec_conformance, architecture, security, tests, correctness, performance, agent_drift
  | reviewing: spec_conformance, architecture, security, tests, correctness, performance, agent_drift
[performance] PASS  No performance concerns at realistic scale
[security] PASS  No vulnerabilities or baseline-pattern violations found
[architecture] PASS  All structural rules in the spec are satisfied
[spec_conformance] PASS  Diff conforms to all spec rules
[tests] WARN  New public function add_widget() has no test
[correctness] PASS  No behavioral bugs found
[agent_drift] WARN  TODO added in same push as the function it annotates

================================================================
claude-multi-agent-review
================================================================

[tests] WARN  New public function add_widget() has no test
  - warn  src/widgets.py:42 [tests.new-public-behavior-has-test]
      add_widget() is new public API. A test covering the happy path
      and the empty-input edge case would close this gap.

[agent_drift] WARN  TODO added in same push as the function it annotates
  - warn  src/widgets.py:55 [agent_drift.todos-need-issue-links]
      `# TODO: handle the multi-widget case` was added alongside the
      implementation. If this is intended for a follow-up, link an
      issue; if it's done, remove the marker.

[spec_conformance] PASS  Diff conforms to all spec rules
[architecture] PASS  All structural rules in the spec are satisfied
[security] PASS  No vulnerabilities or baseline-pattern violations found
[correctness] PASS  No behavioral bugs found
[performance] PASS  No performance concerns at realistic scale

================================================================
PUSH ALLOWED with warnings.
================================================================
To github.com:you/your-repo.git
   a3c1...b9d8  main -> main
```

Any reviewer returning `FAIL` blocks the push. `WARN` is printed and
the push proceeds. `PASS` is shown silently in the summary block.

## Why push, not commit

Agents commit constantly. Codex, Claude Code, and similar tools
iterate across many small commits as they work — adding, backtracking,
refactoring. A quality gate at every commit creates a backlog of
redundant reviews of intermediate states that may not survive to the
final push. It also produces noisier verdicts because per-commit review
sees a function added in commit 1 and deleted in commit 3 as two
separate issues, when the net effect is zero.

The push boundary is the right checkpoint. Commits are a working
journal; pushes are publication. Reviewing at push time means:

- One review pass per logical unit of work, not one per keystroke
- Better signal quality because reviewers see the cumulative diff, not
  intermediate noise
- Lower aggregate cost, even though each invocation is larger
- Alignment with how mature dev workflows treat commits vs. pushes

## Why multiple reviewers

Different reviewers catch different things, and aggregating their
independent verdicts is more useful than a single monolithic review.
Each is one `claude -p` call in its own subprocess with its own system
prompt. The hook ships seven defaults:

| Reviewer | Lens |
|---|---|
| `spec_conformance` | Catch-all for rules in your CLAUDE.md that no specialist lens covers |
| `architecture` | Layering, module boundaries, dependency direction, public-API stability |
| `security` | Vulnerabilities, secrets, dangerous patterns (universal baseline plus spec rules) |
| `tests` | Coverage of new behavior, deleted/skipped tests, test-quality red flags |
| `correctness` | Functional bugs, edge cases, refactor regressions, error-handling holes |
| `performance` | Algorithmic complexity, N+1 queries, hot-path allocations |
| `agent_drift` | AI-agent-authored failure modes: scaffolding leaks, vestigial debug code, broad-except, mid-stream context loss |

You can disable any of them, override their prompts in your repo, or
add your own. See [Configure](#configure).

## For agentic dev workflows

A real workflow today often spans multiple tools: a Codex agent doing
automated dev in one set of git worktrees, a Claude Code session
working in another, and manual commits from a human author landing in
parallel. All of those land in the same remote repo.

The pre-push hook is the only consistent checkpoint where work from
heterogeneous authors converges before publication. Because the hook
fires on every push regardless of who initiated it, the same spec gets
enforced uniformly across human and AI authors. When the pushing
"author" is an agent operating without a human in the loop, the hook
catches what the agent missed and surfaces it as a structured verdict
the agent has to react to.

This is "AI reviewing AI" by design, not by accident. The reviewer
personas know what agent-authored failure modes look like — the
[`agent_drift`](src/personas/agent_drift.md) persona is built around a
pattern library specific to agentic work — and they apply the spec the
human author wrote. The hook is the governance layer between
fast-moving agentic dev and the shared remote.

If you're running a multi-agent dev setup today and looking for a
quality bar that doesn't require trusting any one agent's judgment,
this is what that looks like.

## Install

> A formal `install.sh` is [tracked at issue #6][issue6]. For now, the
> install is manual but small.

[issue6]: https://github.com/Tony-Passavanti/claude-multi-agent-review/issues/6

In any repo you want to enable the hook on:

```sh
# 1. Clone this repo somewhere stable
git clone https://github.com/Tony-Passavanti/claude-multi-agent-review ~/.local/share/claude-multi-agent-review

# 2. From inside the repo you want to protect:
cat > .git/hooks/pre-push <<'EOF'
#!/usr/bin/env sh
exec ~/.local/share/claude-multi-agent-review/bin/claude-multi-agent-review "$@"
EOF
chmod +x .git/hooks/pre-push

# 3. Create a CLAUDE.md at your repo root (start from CLAUDE.md.example
#    in the install dir, or copy this project's CLAUDE.md as a working
#    sample).
```

Requirements:

- Python 3.11+ (stdlib only; no pip dependencies)
- Claude Code installed and authenticated (`claude --version` should work)
- A `CLAUDE.md` file at the root of any repo you push from

To uninstall: delete `.git/hooks/pre-push`.

## Configure

Defaults live in [`config/default.toml`](config/default.toml) and ship
with the hook. To override per-repo, create
`.claude-multi-agent-review.toml` at your repo root. Repo-local keys
win on a per-key basis; unspecified keys inherit shipped defaults.

The knobs that matter most:

```toml
# Which reviewers to run. Remove any you don't want; add new names
# whose persona .md you've placed in .claude-multi-agent-review/personas/.
enabled_personas = [
    "spec_conformance",
    "architecture",
    "security",
    "tests",
    "correctness",
    "performance",
    "agent_drift",
]

# Model to invoke. Sonnet for cost/quality default; bump to Opus for
# higher-stakes repos at higher cost and latency.
model = "claude-sonnet-4-6"

# How to resolve a reviewer that exhausted its retries.
#   "warn" - synthetic WARN, push proceeds (default; avoids hook lockout)
#   "fail" - synthetic FAIL, push blocked (fail-closed for high-stakes repos)
treat_reviewer_failure_as = "warn"

# Soft cap on diff size. Exceeding this emits a meta-WARN but the full
# diff still goes to every reviewer.
max_diff_lines = 5000
```

See [`config/default.toml`](config/default.toml) for the complete
surface, including retry counts, timeouts, and the bypass env var name.

To override a shipped persona's prompt, drop your own
`<name>.md` into `.claude-multi-agent-review/personas/` in your repo.
Repo-local files win over shipped defaults.

## Writing a good `CLAUDE.md`

The spec is the source of truth every reviewer enforces. Without rules
that mean something, the reviewers have nothing to find.

Detailed guidance is [tracked at issue #4][issue4] (`docs/writing-a-spec.md`);
an annotated template is [tracked at issue #2][issue2] (`CLAUDE.md.example`).
For now, this project's own [`CLAUDE.md`](CLAUDE.md) serves as a working
example. Things that work well in practice:

[issue2]: https://github.com/Tony-Passavanti/claude-multi-agent-review/issues/2
[issue4]: https://github.com/Tony-Passavanti/claude-multi-agent-review/issues/4

- **Name your rules.** Each rule gets an id (`output.no-print-in-src`,
  `agent.no-test-deletions-to-pass-ci`). Reviewers cite the id in
  findings; developers can grep for it.
- **State the severity inline.** Mark each rule `FAIL`, `WARN`, or
  `INFO`. Reviewers default to your stated severity.
- **Be specific about what NOT to flag.** Out-of-scope sections are
  load-bearing. They keep reviewers from going off-piste into style
  arguments your team doesn't care about.
- **Include an "Agentic dev rules" section.** This is where the
  `agent_drift` persona earns its keep. Spell out the patterns that
  characterize the agentic workflows in your repo.

## Limitations

This is a tool with real costs and failure modes. Worth being upfront.

**Cost.** Two cost models depending on how Claude Code is authenticated
on the developer's machine:

- **API key (`ANTHROPIC_API_KEY` set):** every push triggers up to N
  reviewer calls × diff size in tokens. Rough estimate at Sonnet
  pricing, 7 reviewers, ~3k-line diff: $0.20–0.50 per push. Scales
  with diff size and reviewer count. Real per-push dollars on the
  Anthropic billing dashboard.
- **Claude Pro/Max subscription (OAuth login):** no per-call dollar
  cost, but each `claude -p` call consumes subscription usage quota.
  Heavy use across multiple worktrees can hit usage limits faster
  than interactive Claude Code sessions consume them.

**Latency.** 7 parallel `claude -p` calls against a moderate-size diff
takes ~30–60 seconds wall time. A push is no longer instant. For
single-file fixes the diff is smaller and faster; for refactors
spanning many files, expect the longer end.

**False positives.** WARN-level findings include "please confirm
intent" cases where the reviewer can see how something *could* be
wrong but can't verify from the diff. Treat WARNs as a prompt for
human attention, not as a definitive defect.

**`--no-verify` bypasses the hook.** Standard git: `git push --no-verify`
skips client-side hooks entirely. So does `CLAUDE_MULTI_AGENT_REVIEW_OVERRIDE=1`.
Either is recoverable from a determined push, including from an agent.
For hard enforcement, pair the hook with a server-side CI check that
runs the same reviewers on PRs and blocks merge — the client-side hook
is the fast feedback loop; the server-side check is the gate.

**Single-shot review.** Reviewers don't follow up. They see the diff
once, return a verdict, and the conversation ends. They can't ask
clarifying questions, look at surrounding files outside the diff, or
re-evaluate after a follow-up commit until the next push.

**Sporadic reviewer timeouts and parse failures.** Real-world: the
occasional `claude -p` call times out at the configured limit (default
180s) or returns prose instead of the requested JSON. The hook's retry
classifier and synthetic-verdict path handle these gracefully, but you
will sometimes see `[reviewer] WARN reviewer failed: ...` instead of a
real verdict. Not a code bug; model-output variability.

**Big diffs degrade review quality.** Reviewers operate within a
finite context window. The `max_diff_lines` config emits a meta-WARN
when the diff exceeds the configured threshold (default 5000 lines);
the full diff still goes through, but findings on unsampled regions
get unreliable. Consider splitting very large changes across pushes.

## The hook reviews itself

This repo's `pre-push` hook is configured to run the same seven
reviewers against any change going to its own `main`. The earliest
commits in `git log` show the recursive validation in action — the
hook caught and fixed a series of real defects in its own code before
the first successful push to GitHub landed. A longer write-up of that
experience is planned separately.

## Development

Branch per change, PR against `main`. Issues track the remaining
scaffolding: [#1 aggregator polish][issue1], [#2 spec example][issue2],
[#4 docs][issue4], [#5 tests][issue5], [#6 installer + CI][issue6],
[#7 read-text guard][issue7].

[issue1]: https://github.com/Tony-Passavanti/claude-multi-agent-review/issues/1
[issue5]: https://github.com/Tony-Passavanti/claude-multi-agent-review/issues/5
[issue7]: https://github.com/Tony-Passavanti/claude-multi-agent-review/issues/7

To exercise the reviewer wrapper against `claude -p` without firing
the full hook, run [`scripts/smoke_review.py`](scripts/smoke_review.py).

## License

[MIT](LICENSE).
