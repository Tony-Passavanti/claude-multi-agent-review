# Dual Claude/Codex Reviewer Migration Plan

## Summary

Stage the migration so the existing Claude reviewer keeps working unchanged while
a new Codex reviewer is added in its own repo section. After the Codex path is
functional and tested, extract only the proven provider-neutral behavior into a
shared core.

Default product model:

- Existing Claude command remains `claude-multi-agent-review`.
- New Codex command is `codex-multi-agent-review`.
- Codex defaults to `REVIEW.md`.
- Claude keeps `CLAUDE.md` compatibility and can later opt into `REVIEW.md`.
- Shared core extraction happens after Codex is stable, not as the first
  refactor.

## Phase 1: Add Codex Beside Claude

- Leave current top-level Claude implementation in place: existing `src/`,
  `config/`, `tests/`, `scripts/`, `CLAUDE.md`, and
  `bin/claude-multi-agent-review` continue to work.
- Create a new `codex/` section containing a copied starting point of the
  reviewer implementation:
  - `codex/src/`
  - `codex/config/`
  - `codex/personas/`
  - `codex/schemas/`
  - `codex/scripts/`
  - `codex/tests/`
  - `codex/README.md`
- Add `bin/codex-multi-agent-review` as the Codex entrypoint.
- Update the root `README.md` into an umbrella overview that explains both
  products and links to Claude/Codex-specific docs.

## Phase 2: Convert the Codex Runner

- In the Codex copy only, replace `claude -p` invocation with `codex exec`.
- Use a read-only, non-interactive Codex posture:
  - `codex exec`
  - `--ephemeral`
  - `--sandbox read-only`
  - `--ask-for-approval never`
  - `--model <configured model>`
  - `--output-schema codex/schemas/verdict.schema.json`
  - disable web search and unnecessary multi-agent behavior for each reviewer
    subprocess.
- Keep one Codex subprocess per persona so existing orchestration semantics stay
  intact.
- Replace Claude envelope parsing with Codex final JSON parsing, while
  preserving the existing `Verdict` / `Finding` validation contract.
- Change Codex defaults:
  - config file: `.codex-multi-agent-review.toml`
  - spec path: `REVIEW.md`
  - default model: `gpt-5.5`
  - bypass env: `CODEX_MULTI_AGENT_REVIEW_OVERRIDE`

## Phase 3: Codex Prompts, Docs, And Tests

- Rewrite Codex persona prompts to say `codex-multi-agent-review` and reference
  `REVIEW.md`.
- Add a JSON Schema matching the verdict contract:
  - `agent_name`
  - `verdict`: `PASS | WARN | FAIL`
  - `summary`
  - `reasoning`
  - `findings[]`
  - finding severity: `info | warn | error`
  - error findings require `file` and integer `line`
- Add Codex-focused tests for:
  - command construction
  - output parsing
  - auth/environment failure classification
  - config loading from `.codex-multi-agent-review.toml`
  - `REVIEW.md` default spec path
  - schema/validator alignment
- Add a Codex smoke script equivalent to the existing Claude smoke script.
- Keep Claude tests passing unchanged.

## Phase 4: Extract Shared Core

After Codex is green and behavior is understood, extract provider-neutral code
into `review_core/`.

Shared core should own:

- git pre-push stdin parsing
- ref/diff payload construction
- `ReviewPayload`, `RefUpdate`, `RefReview`
- `Verdict` and `Finding`
- verdict aggregation and exit-code behavior
- persona/spec file resolution helpers where provider-neutral
- parallel/sequential orchestration
- streaming UI primitives
- common validator tests

Provider packages should keep:

- subprocess command construction
- model/auth defaults
- provider-specific config filenames and env vars
- prompt transport
- output parsing wrappers
- persona wording
- provider README and install docs

Both products then import `review_core`, but retain separate entrypoints and
provider-specific defaults.

## Test Plan

- Run existing Claude unit tests before and after adding Codex to prove no
  regression.
- Run Codex unit tests independently under `codex/tests/`.
- Add shared-core tests during extraction and remove duplicated tests only after
  both products pass against the shared code.
- Acceptance criteria:
  - `bin/claude-multi-agent-review` still behaves as before.
  - `bin/codex-multi-agent-review` runs Codex reviewers in parallel and returns
    the same aggregate PASS/WARN/FAIL semantics.
  - Claude and Codex can be installed independently from the same repo.
  - No Codex migration step requires current Claude users to rename config,
    hooks, or spec files.

## Assumptions

- We will not break existing Claude behavior during the first Codex migration.
- `REVIEW.md` is the Codex default spec filename.
- Shared core extraction is a second-stage refactor after the Codex
  implementation is working.
- The repo remains stdlib-only for production Python unless a later packaging
  decision explicitly changes that.
