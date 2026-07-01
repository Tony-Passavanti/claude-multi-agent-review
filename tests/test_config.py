"""Tests for src/config.py.

Covers the validation gauntlet in `_config_from_dict` (the most complex
pure function in the module) and the high-level `load()` flow including
shipped-defaults reading and repo-local override merging. Filesystem-
touching paths are exercised via tmp_path fixtures, no real disk
mocking needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Config, ReviewerGate, _config_from_dict, load


# --- _config_from_dict happy path ------------------------------------------

def _good_data() -> dict[str, object]:
    """A minimal-valid dict matching the shipped schema."""
    return {
        "spec_path": "CLAUDE.md",
        "default_branch": "",
        "enabled_personas": ["spec_conformance"],
        "model": "claude-sonnet-4-6",
        "parallel": True,
        "review_tags": False,
        "override_env": "CLAUDE_MULTI_AGENT_REVIEW_OVERRIDE",
        "reviewer_timeout_seconds": 180,
        "reviewer_retries": 1,
        "treat_reviewer_failure_as": "warn",
        "max_diff_lines": 5000,
    }


def _from_dict(data: dict[str, object]) -> Config:
    return _config_from_dict(
        data, install_root=Path("."), repo_root=Path("."),
    )


def test_config_from_dict_happy_path() -> None:
    cfg = _from_dict(_good_data())
    assert cfg.spec_path == Path("CLAUDE.md")
    assert cfg.enabled_personas == ["spec_conformance"]
    assert cfg.treat_reviewer_failure_as == "warn"
    assert cfg.max_diff_lines == 5000
    assert cfg.extra == {}


# --- missing required keys -------------------------------------------------

def test_missing_required_key_raises() -> None:
    data = _good_data()
    del data["model"]
    with pytest.raises(ValueError, match="model"):
        _from_dict(data)


def test_missing_multiple_keys_listed_in_error() -> None:
    data = _good_data()
    del data["model"]
    del data["parallel"]
    with pytest.raises(ValueError) as exc:
        _from_dict(data)
    msg = str(exc.value)
    assert "model" in msg
    assert "parallel" in msg


# --- type mismatches -------------------------------------------------------

def test_wrong_type_str_field() -> None:
    data = _good_data()
    data["spec_path"] = 42  # type: ignore[assignment]
    with pytest.raises(ValueError, match="spec_path"):
        _from_dict(data)


def test_wrong_type_int_field() -> None:
    data = _good_data()
    data["max_diff_lines"] = "5000"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="max_diff_lines"):
        _from_dict(data)


def test_wrong_type_bool_field() -> None:
    data = _good_data()
    data["parallel"] = "true"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="parallel"):
        _from_dict(data)


def test_wrong_type_list_field() -> None:
    data = _good_data()
    data["enabled_personas"] = "spec_conformance"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="enabled_personas"):
        _from_dict(data)


# --- bool-vs-int guard ------------------------------------------------------

def test_bool_rejected_where_int_expected() -> None:
    # `bool` subclasses `int` in Python — without an explicit guard,
    # isinstance(True, int) returns True and a misspelled config value
    # would silently pass validation.
    data = _good_data()
    data["max_diff_lines"] = True  # type: ignore[assignment]
    with pytest.raises(ValueError, match="expected int, got bool"):
        _from_dict(data)


def test_bool_rejected_for_reviewer_retries() -> None:
    data = _good_data()
    data["reviewer_retries"] = False  # type: ignore[assignment]
    with pytest.raises(ValueError, match="expected int, got bool"):
        _from_dict(data)


# --- enabled_personas list checks ------------------------------------------

def test_empty_personas_rejected() -> None:
    data = _good_data()
    data["enabled_personas"] = []
    with pytest.raises(ValueError, match="enabled_personas.*not be empty"):
        _from_dict(data)


def test_non_string_persona_rejected() -> None:
    data = _good_data()
    data["enabled_personas"] = ["spec_conformance", 42, "security"]  # type: ignore[list-item]
    with pytest.raises(ValueError, match=r"enabled_personas\[1\]"):
        _from_dict(data)


# --- treat_reviewer_failure_as enum check ----------------------------------

def test_invalid_failure_mode_rejected() -> None:
    data = _good_data()
    data["treat_reviewer_failure_as"] = "block"
    with pytest.raises(ValueError, match="treat_reviewer_failure_as"):
        _from_dict(data)


def test_valid_failure_modes_accepted() -> None:
    for mode in ("warn", "fail"):
        data = _good_data()
        data["treat_reviewer_failure_as"] = mode
        cfg = _from_dict(data)
        assert cfg.treat_reviewer_failure_as == mode


# --- forward-compat unknown keys -------------------------------------------

def test_unknown_keys_go_to_extra(capsys) -> None:
    data = _good_data()
    data["future_feature"] = "hello"
    data["another_new_thing"] = 42
    cfg = _from_dict(data)
    assert cfg.extra == {"future_feature": "hello", "another_new_thing": 42}


def test_unknown_keys_logged_to_stderr(capsys) -> None:
    data = _good_data()
    data["future_feature"] = "hello"
    _from_dict(data)
    captured = capsys.readouterr()
    assert "future_feature" in captured.err
    assert "newer version" in captured.err


def test_unknown_keys_do_not_appear_in_real_fields() -> None:
    data = _good_data()
    data["future_feature"] = "hello"
    cfg = _from_dict(data)
    # Real config fields are untouched
    assert cfg.spec_path == Path("CLAUDE.md")


# --- install_root / repo_root threading -----------------------------------

def test_install_root_and_repo_root_passed_through() -> None:
    install = Path("/some/install")
    repo = Path("/other/repo")
    cfg = _config_from_dict(
        _good_data(), install_root=install, repo_root=repo,
    )
    assert cfg.install_root == install
    assert cfg.repo_root == repo


# --- load() integration tests via tmp_path --------------------------------

def _write_shipped_default(tmp_path: Path, content: str | None = None) -> None:
    """Create a shipped-defaults TOML at tmp_path/config/default.toml."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    if content is None:
        content = (
            'spec_path = "CLAUDE.md"\n'
            'default_branch = ""\n'
            'enabled_personas = ["spec_conformance"]\n'
            'model = "claude-sonnet-4-6"\n'
            'parallel = true\n'
            'review_tags = false\n'
            'override_env = "CLAUDE_MULTI_AGENT_REVIEW_OVERRIDE"\n'
            "reviewer_timeout_seconds = 180\n"
            "reviewer_retries = 1\n"
            'treat_reviewer_failure_as = "warn"\n'
            "max_diff_lines = 5000\n"
        )
    (config_dir / "default.toml").write_text(content, encoding="utf-8")


def test_load_with_only_shipped_defaults(tmp_path: Path) -> None:
    _write_shipped_default(tmp_path)
    cfg = load(install_root=tmp_path, repo_root=tmp_path)
    assert cfg.enabled_personas == ["spec_conformance"]
    assert cfg.max_diff_lines == 5000


def test_load_missing_shipped_defaults_raises(tmp_path: Path) -> None:
    # No config/default.toml created.
    with pytest.raises(FileNotFoundError, match="shipped default config"):
        load(install_root=tmp_path, repo_root=tmp_path)


def test_load_malformed_shipped_defaults_raises(tmp_path: Path) -> None:
    _write_shipped_default(tmp_path, content="this is = not valid [toml\n")
    with pytest.raises(ValueError, match="shipped default config.*malformed"):
        load(install_root=tmp_path, repo_root=tmp_path)


def test_load_repo_local_overrides_shipped(tmp_path: Path) -> None:
    _write_shipped_default(tmp_path)
    (tmp_path / ".claude-multi-agent-review.toml").write_text(
        "max_diff_lines = 9999\nparallel = false\n",
        encoding="utf-8",
    )
    cfg = load(install_root=tmp_path, repo_root=tmp_path)
    # Overridden:
    assert cfg.max_diff_lines == 9999
    assert cfg.parallel is False
    # Inherited from shipped:
    assert cfg.enabled_personas == ["spec_conformance"]


def test_load_malformed_repo_local_raises(tmp_path: Path) -> None:
    _write_shipped_default(tmp_path)
    (tmp_path / ".claude-multi-agent-review.toml").write_text(
        "invalid [toml syntax",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="repo-local config.*malformed"):
        load(install_root=tmp_path, repo_root=tmp_path)


def test_load_no_repo_local_uses_defaults_only(tmp_path: Path) -> None:
    _write_shipped_default(tmp_path)
    # No .claude-multi-agent-review.toml — should not error.
    cfg = load(install_root=tmp_path, repo_root=tmp_path)
    assert cfg.max_diff_lines == 5000


# --- reviewer_gates validation ---------------------------------------------

def test_reviewer_gates_default_empty_when_absent() -> None:
    # `_good_data()` deliberately omits reviewer_gates — the validator
    # treats it as optional and defaults to an empty list.
    cfg = _from_dict(_good_data())
    assert cfg.reviewer_gates == []


def test_reviewer_gates_parsed_into_dataclasses() -> None:
    data = _good_data()
    data["reviewer_gates"] = [
        {"name": "docs", "patterns": ["*.md"], "personas": ["a", "b"]},
        {"name": "tests", "patterns": ["tests/*"], "personas": ["a"]},
    ]
    cfg = _from_dict(data)
    assert len(cfg.reviewer_gates) == 2
    assert isinstance(cfg.reviewer_gates[0], ReviewerGate)
    assert cfg.reviewer_gates[0].name == "docs"
    assert cfg.reviewer_gates[0].patterns == ["*.md"]
    assert cfg.reviewer_gates[0].personas == ["a", "b"]
    assert cfg.reviewer_gates[1].name == "tests"


def test_reviewer_gates_not_list_rejected() -> None:
    data = _good_data()
    data["reviewer_gates"] = "docs"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="reviewer_gates.*expected list"):
        _from_dict(data)


def test_reviewer_gates_entry_not_table_rejected() -> None:
    data = _good_data()
    data["reviewer_gates"] = ["just a string"]
    with pytest.raises(ValueError, match=r"reviewer_gates\[0\].*expected table"):
        _from_dict(data)


def test_reviewer_gates_missing_required_key_rejected() -> None:
    data = _good_data()
    data["reviewer_gates"] = [{"name": "x", "patterns": ["*"]}]  # no personas
    with pytest.raises(ValueError, match="personas"):
        _from_dict(data)


def test_reviewer_gates_empty_patterns_rejected() -> None:
    data = _good_data()
    data["reviewer_gates"] = [
        {"name": "x", "patterns": [], "personas": ["a"]},
    ]
    with pytest.raises(ValueError, match=r"patterns.*not be empty"):
        _from_dict(data)


def test_reviewer_gates_empty_personas_rejected() -> None:
    data = _good_data()
    data["reviewer_gates"] = [
        {"name": "x", "patterns": ["*"], "personas": []},
    ]
    with pytest.raises(ValueError, match=r"personas.*not be empty"):
        _from_dict(data)


def test_reviewer_gates_non_string_pattern_rejected() -> None:
    data = _good_data()
    data["reviewer_gates"] = [
        {"name": "x", "patterns": [42], "personas": ["a"]},
    ]
    with pytest.raises(ValueError, match=r"patterns\[0\]"):
        _from_dict(data)


def test_reviewer_gates_wrong_field_type_rejected() -> None:
    data = _good_data()
    data["reviewer_gates"] = [
        {"name": "x", "patterns": "not a list", "personas": ["a"]},
    ]
    with pytest.raises(ValueError, match="patterns.*expected list"):
        _from_dict(data)


def test_reviewer_gates_unknown_inner_key_tolerated() -> None:
    # Forward-compat: a future hook version may add fields to gate
    # entries (e.g. "notes"). An older validator must not crash on them.
    data = _good_data()
    data["reviewer_gates"] = [
        {
            "name": "x", "patterns": ["*"], "personas": ["a"],
            "notes": "added in v2",
        },
    ]
    cfg = _from_dict(data)
    assert len(cfg.reviewer_gates) == 1
    assert cfg.reviewer_gates[0].name == "x"


# --- load() / shipped defaults integration --------------------------------

def test_load_repo_local_forward_compat_keys(tmp_path: Path) -> None:
    _write_shipped_default(tmp_path)
    (tmp_path / ".claude-multi-agent-review.toml").write_text(
        'future_key = "new feature"\n',
        encoding="utf-8",
    )
    cfg = load(install_root=tmp_path, repo_root=tmp_path)
    assert cfg.extra == {"future_key": "new feature"}


# --- metrics keys (issue #19) ----------------------------------------------

def test_metrics_defaults_when_absent() -> None:
    # _good_data() omits the metrics keys — they're optional and default
    # to on (opt-out) at the shipped default path.
    cfg = _from_dict(_good_data())
    assert cfg.metrics_enabled is True
    assert cfg.metrics_path == ".claude-multi-agent-review/metrics.jsonl"


def test_metrics_keys_not_treated_as_unknown(capsys) -> None:
    # Recognized optional keys must NOT land in `extra` or trigger the
    # unknown-key stderr notice.
    data = _good_data()
    data["metrics_enabled"] = False
    data["metrics_path"] = "logs/m.jsonl"
    cfg = _from_dict(data)
    assert cfg.metrics_enabled is False
    assert cfg.metrics_path == "logs/m.jsonl"
    assert cfg.extra == {}
    assert "metrics_enabled" not in capsys.readouterr().err


def test_metrics_enabled_wrong_type_rejected() -> None:
    data = _good_data()
    data["metrics_enabled"] = "yes"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="metrics_enabled.*expected bool"):
        _from_dict(data)


def test_metrics_path_wrong_type_rejected() -> None:
    data = _good_data()
    data["metrics_path"] = 42  # type: ignore[assignment]
    with pytest.raises(ValueError, match="metrics_path.*expected str"):
        _from_dict(data)
