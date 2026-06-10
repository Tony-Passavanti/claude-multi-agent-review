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

### Python unit tests (pytest)

| File | Covers |
|---|---|
| `test_aggregate.py` | exit-code rules, verdict ordering, report formatting |
| `test_config.py` | TOML loader, validation gauntlet, forward-compat |
| `test_hook.py` | stdin parsing, zero-SHA detection, payload formatting, RefUpdate properties |
| `test_reviewer.py` | JSON extraction, verdict/finding validators, failure classifier |
| `test_orchestrate.py` | persona resolution, missing-persona synthetic verdicts, diff-size meta-WARN, dispatch (parallel/sequential), reviewer-crash safety net |

`conftest.py` provides shared fixtures: `install_root`, `repo_root`,
`make_config`, `make_persona`, `make_local_persona`.

### Shell integration tests

| File | Covers |
|---|---|
| `test_src_shadowing.sh` | regression guard for PR #8's `python -P` + PYTHONPATH fix: shim invoked from a tmp consuming repo with a conflicting top-level `src/` must still exit cleanly |

Run shell tests directly:

```sh
sh tests/test_src_shadowing.sh
```

The full test suite (Python + shell) will be wired into CI under
issue #6.
