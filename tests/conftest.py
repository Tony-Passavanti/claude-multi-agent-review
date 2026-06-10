"""Shared pytest fixtures for the test suite.

Most useful for tests that need a synthetic install_root / repo_root
layout and a Config built against it — primarily test_orchestrate.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Config


@pytest.fixture
def install_root(tmp_path: Path) -> Path:
    """A synthetic install location with the directories the hook expects.

    Layout:
        <tmp>/install/
            src/personas/   (empty; tests add personas via make_persona)
    """
    root = tmp_path / "install"
    (root / "src" / "personas").mkdir(parents=True)
    return root


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A synthetic consuming repo with a minimal spec file already present."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "CLAUDE.md").write_text(
        "# Test spec\n\nrule.example — do nothing wrong. FAIL.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def make_persona(install_root: Path):
    """Factory: create a persona .md in the shipped location.

    Returns the path that orchestrate._resolve_persona_path would find
    as the shipped (non-override) candidate.
    """
    def _factory(name: str, content: str | None = None) -> Path:
        path = install_root / "src" / "personas" / f"{name}.md"
        path.write_text(
            content if content is not None else f"# {name} persona\n",
            encoding="utf-8",
        )
        return path
    return _factory


@pytest.fixture
def make_local_persona(repo_root: Path):
    """Factory: create a repo-local persona override.

    Returns the path that orchestrate._resolve_persona_path should
    prefer over the shipped default with the same name.
    """
    def _factory(name: str, content: str | None = None) -> Path:
        local_dir = repo_root / ".claude-multi-agent-review" / "personas"
        local_dir.mkdir(parents=True, exist_ok=True)
        path = local_dir / f"{name}.md"
        path.write_text(
            content if content is not None else f"# local {name} persona\n",
            encoding="utf-8",
        )
        return path
    return _factory


@pytest.fixture
def make_config(install_root: Path, repo_root: Path):
    """Factory: build a Config with sensible defaults for orchestrate tests.

    Defaults match shipped behavior except `parallel=False` (sequential
    dispatch is deterministic — verdicts arrive in submission order,
    which makes assertions simpler). Pass kwargs to override.
    """
    def _factory(**overrides: object) -> Config:
        defaults: dict[str, object] = dict(
            spec_path=Path("CLAUDE.md"),
            default_branch="",
            enabled_personas=[],
            model="claude-sonnet-4-6",
            parallel=False,
            review_tags=False,
            override_env="CLAUDE_MULTI_AGENT_REVIEW_OVERRIDE",
            reviewer_timeout_seconds=180,
            reviewer_retries=1,
            treat_reviewer_failure_as="warn",
            max_diff_lines=5000,
            install_root=install_root,
            repo_root=repo_root,
        )
        defaults.update(overrides)
        return Config(**defaults)  # type: ignore[arg-type]
    return _factory
