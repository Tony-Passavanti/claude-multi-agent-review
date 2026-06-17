# claude-multi-agent-review — project spec

This file is the spec the pre-push hook's reviewers enforce against every
push to this repo. It is read by all seven personas listed in
`config/default.toml`. When a rule below is violated, the relevant
persona should emit a finding citing the rule id.

The hook reviews itself on push. Treat this file as a working contract.

## Stack and dependencies

- **stack.python-version** — Python 3.11+ required. Use 3.11 stdlib
  features (e.g. `tomllib`, `Literal`, PEP-604 union syntax). FAIL.

- **stack.no-third-party-deps-in-src** — `src/` MUST NOT import any
  third-party package. Stdlib only. The hook is a single-file-install
  tool; adding deps breaks that promise. FAIL.

- **stack.dev-deps-allowed-in-tests** — `tests/` may use third-party
  test frameworks (`pytest`, etc.) but those deps must be declared in a
  documented dev-requirements file, not silently imported. WARN if
  undeclared.

## Module layout

- **layout.src-is-the-package** — All production code lives under `src/`.
  `python -m src` is the only supported entry point. FAIL on production
  code added outside `src/`.

- **layout.module-roles** — Module responsibilities are:
  - `src/__main__.py` — argv/stdin parsing, dispatch to `hook.run`
  - `src/hook.py` — stdin parsing, diff computation, payload formatting,
    top-level `run()`
  - `src/reviewer.py` — single `claude -p` invocation, retry/classify,
    synthetic verdicts on exhaustion
  - `src/orchestrate.py` — parallel/sequential dispatch, streaming UI
  - `src/aggregate.py` — verdict schema (`Verdict`, `Finding`), exit-code
    rules, report formatting
  - `src/config.py` — config dataclass and loader
  - `src/metrics.py` — per-reviewer token/cost capture from the `claude -p`
    envelope (`ReviewerUsage` + pure parse/aggregate; no I/O)
  - `src/personas/*.md` — reviewer prompts
  Adding a new module requires a documented role. WARN on unclear placement.

- **layout.persona-files-are-content** — Persona files are `.md`, not
  Python. They are loaded from disk and fed to `claude -p` as system
  prompts. Never inline a persona as a Python string. FAIL.

- **layout.bin-shim-is-thin** — `bin/claude-multi-agent-review` is a POSIX `sh`
  shim. It resolves paths and execs `python -m src`. Business logic
  belongs in Python, not the shim. FAIL on shim that does more than
  resolve-and-exec.

## Public API stability

- **api.public-symbols** — These names are public and consumed by other
  modules or tests:
  - `hook.run`, `hook.parse_stdin`, `hook.build_payload`,
    `hook.format_diff_payload`, `hook.RefUpdate`, `hook.RefReview`,
    `hook.ReviewPayload`, `hook.is_zero_sha`
  - `reviewer.review`
  - `orchestrate.review_all`
  - `aggregate.aggregate`, `aggregate.Verdict`, `aggregate.Finding`
  - `config.Config`, `config.load`
  - `metrics.ReviewerUsage`, `metrics.usage_from_stdout`, `metrics.combine`
  Renaming or changing the signature of any of these is a FAIL unless
  the diff also updates every caller.

- **api.underscored-is-private** — Names starting with `_` are internal.
  Other modules MUST NOT import them. WARN.
  *Exception:* Test modules under `tests/` are exempt. Unit-testing
  internal validators, parsers, and classifiers is standard practice
  and is required for adequate coverage of code paths the public API
  does not expose directly (e.g. `_extract_first_json_object`,
  `_verdict_from_raw`, `_classify_subprocess_failure`).

## Verdict schema source of truth

- **schema.three-place-sync** — The verdict schema is defined in THREE
  places that MUST stay in lockstep:
  1. `aggregate.Verdict` + `aggregate.Finding` dataclasses (the dataclass
     definition is the canonical structure)
  2. `reviewer._verdict_from_raw` + `reviewer._finding_from_raw` (the
     runtime validator)
  3. The "Required output" section of every persona `.md` (the prompt
     that asks the model to produce that shape)
  Any change to one MUST update the other two. FAIL.

- **schema.severity-error-requires-location** — Findings with
  `severity: "error"` MUST have both `file` (string) and `line` (int).
  This is enforced in `reviewer._finding_from_raw` and stated in every
  persona prompt. FAIL on diffs that loosen this rule in any one of
  the three places.

- **schema.verdict-levels** — Verdict levels are exactly `PASS`, `WARN`,
  `FAIL` (uppercase). Failure classes (`transient`, `parse`, `auth`,
  `environment`) are a separate axis used only by `reviewer.py`. Don't
  conflate them. FAIL.

## Persona file structure

- **persona.template-conformance** — Every persona `.md` follows the
  same section structure as `src/personas/spec_conformance.md`:
  identity → job → stdin format → how-to-review → what-not-to-flag →
  verdict levels → required output → field requirements → hard rules.
  Drift from this template makes personas harder for users to fork.
  WARN.

- **persona.agent-name-matches-filename** — A persona file at
  `src/personas/<name>.md` MUST set `agent_name` to `"<name>"` in its
  required-output schema. `reviewer._verdict_from_raw` validates this.
  FAIL.

## Logging and output

- **output.stdout-reserved-for-aggregator** — Only `aggregate.aggregate`'s
  formatted report goes to stdout. Everything else (header line,
  streaming verdicts, spinner, error messages, bypass notices) MUST go
  to stderr. This is so users can pipe `git push 2>/dev/null` and still
  get a clean final report, or capture stdout into a file separately
  from the in-flight UI. FAIL.

- **output.no-print-in-src-except-aggregator** — Production code under
  `src/` MUST NOT use bare `print()` calls except in `aggregate.py` (for
  the final report). All other output goes through `sys.stderr` with
  `print(..., file=sys.stderr, flush=True)`. FAIL.

- **output.streaming-via-lock** — Any code writing to stderr while the
  spinner thread is running MUST acquire `orchestrate._stream_lock`
  first. Bypassing the lock will garble the output. FAIL.

## Concurrency

- **concurrency.thread-safety-orchestrate** — `orchestrate.py` uses a
  `ThreadPoolExecutor` for parallel reviewers and a background thread
  for the spinner. Shared mutable state (`_Spinner._in_progress`,
  `_Spinner._line_drawn`, `_Spinner._frame_idx`) is protected by
  `_stream_lock`. Any new mutable state shared across these threads
  MUST use the same lock. FAIL.

- **concurrency.no-async-without-justification** — This codebase is
  synchronous with `concurrent.futures` for parallelism. Adding `asyncio`
  or `async`/`await` requires a justification in the commit message — it
  changes the concurrency model significantly. WARN.

## Error handling

- **errors.reviewer-never-raises-on-failure** — `reviewer.review()`
  MUST always return a `Verdict`. Any failure mode (subprocess error,
  parse failure, auth failure) becomes a synthetic verdict whose level
  is driven by `config.treat_reviewer_failure_as`. The orchestrator
  depends on this property. FAIL on code paths that allow `review()`
  to raise.

- **errors.hook-internal-exit-code-2** — If the hook itself fails
  (missing spec, missing config, etc.) the process exits with code 2,
  not 1. Code 1 is reserved for "push blocked by reviewer FAIL". Code 2
  means "hook is broken; allowing the push to avoid lockout". FAIL on
  conflating them.

- **errors.no-bare-except** — `except:` and `except Exception:` are
  prohibited in `src/` except where there is a documented reason in a
  comment. The pattern `except Exception: pass` is always FAIL.

## Type hints

- **types.public-functions-typed** — All public functions and methods
  in `src/` MUST have type hints on parameters and return values.
  Internal helpers (underscore-prefixed) may skip return-type hints
  where the inference is obvious. WARN on missing hints on internal
  helpers; FAIL on missing hints on public symbols.

- **types.dataclass-fields-typed** — All `@dataclass` field declarations
  MUST be typed. This is enforced by the language but worth being
  explicit. FAIL.

## Tests

- **tests.test-files-in-tests-dir** — Test files live in `tests/`, named
  `test_<module>.py` for unit tests or `test_<scenario>.sh` for shell
  integration tests. FAIL on tests placed elsewhere.

- **tests.no-skip-without-justification** — `@pytest.mark.skip` or
  equivalent MUST include a reason as the decorator argument, and the
  reason MUST cite either an issue/ticket or a technical blocker.
  "Failing locally" is not a justification. FAIL.

- **tests.new-public-behavior-has-test** — New public functions added
  to `src/` SHOULD have at least one test in `tests/` exercising them.
  WARN if missing (we recognize the test suite is still being built up).

## Agentic dev rules

This project is itself developed across multiple agentic workflows. The
following rules catch the failure modes characteristic of agent-authored
code. They are the agent_drift persona's main scope.

- **agent.no-scaffolding-leak** — `pass  # placeholder`,
  `raise NotImplementedError()` in code that is called from production
  paths, sample/example values in production paths (`"REPLACE_ME"`,
  `"your-api-key-here"`), and similar scaffolding MUST be removed before
  push. FAIL.

- **agent.no-stub-without-issue-link** — `NotImplementedError` is allowed
  when (a) it's in a function clearly documented as a stub and (b) the
  comment above it cites the issue that will implement it. Example:
  `# Issue #42 will implement this`. FAIL otherwise.

- **agent.no-test-deletions-to-pass-ci** — Tests that previously passed
  MUST NOT be deleted, skipped, or weakened in the same push that
  changed the code they exercised. FAIL.

- **agent.no-boundary-bypass** — A direct call from one module into
  another module's `_`-prefixed internals is a boundary bypass. Use the
  public API. FAIL.
  *Exception:* Test modules under `tests/` are exempt for the same
  reasons described under `api.underscored-is-private`. The boundary-
  bypass concern is about production code reaching into other
  production code's internals to avoid the public API; tests
  legitimately need access to internals to verify them.

- **agent.no-unauthorized-deps** — Adding a third-party dependency to
  `src/` is always FAIL (see `stack.no-third-party-deps-in-src`). The
  agent_drift persona specifically watches for this because agents
  sometimes reach for a familiar library without checking the project's
  stack rules.

- **agent.no-vestigial-debug** — `print()` for debugging, `console.log`,
  `debug=True` defaults, `verbose=True` defaults, commented-out code
  blocks with no explanation. All FAIL.

- **agent.todos-need-issue-links** — `# TODO:` and `# FIXME:` comments
  added in this push MUST reference a task number or issue. Standalone
  TODOs are WARN; long-lived TODOs without a link added during
  intermediate iteration are FAIL.

## Performance considerations

- **perf.no-n-plus-one-in-orchestration** — `orchestrate.py` dispatches
  reviewers concurrently. Any new code that runs sequentially in a way
  the user pays for (e.g., per-reviewer setup that could be hoisted out
  of the loop) is a WARN.

- **perf.no-premature-optimization** — Performance findings should
  identify real, measurable issues. "This could be slightly faster" is
  not a finding. WARN on changes that add complexity for marginal gain.

## Documentation

- **docs.readme-mirrors-truth** — The README's claims about how the hook
  works MUST match what the code actually does. If you change behavior,
  update the README in the same push. WARN.

- **docs.framing** — The project's framing is "a quality checkpoint
  between agentic dev workflows and the shared remote" — NOT "an AI
  code review tool." Public-facing copy that drifts to the latter
  framing is WARN.

## Out of scope

- Style and formatting beyond what the rules above call out. No opinion
  on import order, line length, blank-line conventions, etc.
- Programming language choice. Python is the choice; that's not up for
  debate per push.
- The project's framing and positioning (those are decided; spec is for
  code rules, not strategy).
