# tests/

Unit and integration tests for claude-multi-agent-review.

## Setup

Install the dev dependencies (pytest) into your environment:

```sh
pip install -r tests/dev-requirements.txt
```

The production code in `src/` uses only the standard library; pytest is
needed only to run these tests.

## Running

From the repo root:

```sh
pytest tests/
```

Or to run a single file or a single test:

```sh
pytest tests/test_aggregate.py
pytest tests/test_aggregate.py::test_aggregate_blocks_on_fail
```

## Layout

| File | Covers |
|---|---|
| `test_aggregate.py` | exit-code rules, verdict ordering, report formatting |
| `test_config.py` | TOML loader, validation gauntlet, forward-compat |
| `test_hook.py` | stdin parsing, zero-SHA detection, payload formatting, RefUpdate properties |
| `test_reviewer.py` | JSON extraction, verdict/finding validators, failure classifier |

Integration tests that exercise the full hook flow against synthetic
repos (e.g. `test_src_shadowing.sh`) live alongside these and run via
`pytest` for Python tests or directly via `sh` for shell tests.

### Known coverage gaps

- `src/orchestrate.py` has no unit tests yet. The module's
  `review_all()` accepts an injectable `reviewer_fn` parameter
  specifically for testability; coverage of persona resolution,
  missing-persona synthetic verdicts, diff-size meta-WARN, and
  parallel/sequential dispatch is in scope for PR-B of issue #5.
