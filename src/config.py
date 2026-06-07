"""Config loader.

Merges shipped defaults (`<install_root>/config/default.toml`) with the
consuming repo's `.claude-multi-agent-review.toml`. All keys have safe defaults
shipped, so a freshly installed hook works with zero repo-local config.

Repo-local keys override shipped defaults on a per-key basis. Unknown keys
in the repo-local file are surfaced as a stderr notice and stashed in
`Config.extra` — this gives forward/backward compatibility a chance: a
newer config file (with keys this version doesn't understand) won't crash
the hook, and an older config (missing keys this version expects) inherits
from the shipped defaults.

`config.load()` raises on any structural problem (missing shipped defaults,
malformed TOML, type mismatch on a known key, invalid enum value). Callers
should catch and exit 2 — "hook is broken, push allowed to avoid lockout."
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Schema for known config keys: name -> expected Python type. Used by the
# validator to produce specific error messages. Keep in sync with both
# `config/default.toml` and the Config dataclass below.
_SCHEMA: dict[str, type] = {
    "spec_path": str,
    "default_branch": str,
    "enabled_personas": list,
    "model": str,
    "parallel": bool,
    "review_tags": bool,
    "override_env": str,
    "reviewer_timeout_seconds": int,
    "reviewer_retries": int,
    "treat_reviewer_failure_as": str,
    "max_diff_lines": int,
}

_FAILURE_MODES = ("warn", "fail")


@dataclass(frozen=True)
class Config:
    spec_path: Path
    default_branch: str  # empty string => auto-detect
    enabled_personas: list[str]
    model: str
    parallel: bool
    review_tags: bool
    override_env: str
    reviewer_timeout_seconds: int
    reviewer_retries: int
    treat_reviewer_failure_as: str  # "warn" | "fail"
    max_diff_lines: int
    install_root: Path
    repo_root: Path
    extra: dict[str, object] = field(default_factory=dict)


def load(*, install_root: Path, repo_root: Path) -> Config:
    """Resolve effective config for this push.

    Reads shipped defaults, optionally merges repo-local overrides, validates,
    and returns a frozen `Config`. Raises on structural problems; callers
    should treat exceptions as "hook is broken" and exit 2.
    """
    defaults_path = install_root / "config" / "default.toml"
    if not defaults_path.is_file():
        raise FileNotFoundError(
            f"shipped default config missing at {defaults_path}. "
            "Reinstall claude-multi-agent-review."
        )

    with defaults_path.open("rb") as f:
        try:
            defaults = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(
                f"shipped default config at {defaults_path} is malformed: {e}"
            ) from e

    local_path = repo_root / ".claude-multi-agent-review.toml"
    if local_path.is_file():
        with local_path.open("rb") as f:
            try:
                local = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise ValueError(
                    f"repo-local config at {local_path} is malformed: {e}"
                ) from e
        merged = {**defaults, **local}
    else:
        merged = defaults

    return _config_from_dict(
        merged, install_root=install_root, repo_root=repo_root,
    )


def _config_from_dict(
    data: dict[str, Any],
    *,
    install_root: Path,
    repo_root: Path,
) -> Config:
    # Required keys: every key in the schema must appear (shipped defaults
    # supply all of them, so this only fires if the shipped file is broken
    # or a future merge logic regresses).
    missing = [k for k in _SCHEMA if k not in data]
    if missing:
        raise ValueError(
            f"config missing required key(s): {', '.join(missing)}"
        )

    # Type-check every known key. `bool` is a subclass of `int` in Python,
    # so check bool before int to avoid silently accepting `true` where an
    # integer was expected (this matters for keys like reviewer_retries).
    for key, expected in _SCHEMA.items():
        value = data[key]
        if expected is int and isinstance(value, bool):
            raise ValueError(
                f"config key {key!r}: expected int, got bool"
            )
        if not isinstance(value, expected):
            raise ValueError(
                f"config key {key!r}: expected {expected.__name__}, "
                f"got {type(value).__name__}"
            )

    # enabled_personas: list[str], non-empty, every element a string.
    personas = data["enabled_personas"]
    if not personas:
        raise ValueError("config key 'enabled_personas' must not be empty")
    for i, item in enumerate(personas):
        if not isinstance(item, str):
            raise ValueError(
                f"enabled_personas[{i}]: expected string, got {type(item).__name__}"
            )

    # Enum check on treat_reviewer_failure_as.
    failure_mode = data["treat_reviewer_failure_as"]
    if failure_mode not in _FAILURE_MODES:
        raise ValueError(
            f"config key 'treat_reviewer_failure_as' must be one of "
            f"{_FAILURE_MODES}, got {failure_mode!r}"
        )

    # Forward-compat: unknown keys go to `extra` and are surfaced as a
    # one-line notice. Not an error — a newer config file should still be
    # readable by an older hook (with the new features silently inactive).
    extra = {k: v for k, v in data.items() if k not in _SCHEMA}
    if extra:
        for k in extra:
            print(
                f"claude-multi-agent-review: unknown config key {k!r} ignored "
                "(may be from a newer version of the hook)",
                file=sys.stderr,
            )

    return Config(
        spec_path=Path(data["spec_path"]),
        default_branch=data["default_branch"],
        enabled_personas=list(personas),
        model=data["model"],
        parallel=data["parallel"],
        review_tags=data["review_tags"],
        override_env=data["override_env"],
        reviewer_timeout_seconds=data["reviewer_timeout_seconds"],
        reviewer_retries=data["reviewer_retries"],
        treat_reviewer_failure_as=failure_mode,
        max_diff_lines=data["max_diff_lines"],
        install_root=install_root,
        repo_root=repo_root,
        extra=extra,
    )
