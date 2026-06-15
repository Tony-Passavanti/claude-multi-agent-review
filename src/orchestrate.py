"""Reviewer dispatch + streaming progress UI.

`review_all()` is the entry point. It:
  - reads the spec from disk
  - resolves each enabled persona to a .md file (repo-local override wins
    over the shipped default)
  - emits a meta-WARN when the diff exceeds the configured size threshold
  - synthesizes a verdict for any enabled persona whose file is missing,
    rather than crashing the push
  - dispatches the remaining reviewers in parallel (or sequentially per
    config) and streams each verdict to stderr as it arrives
  - returns the collected list[Verdict] for the aggregator to format

Stderr is the streaming channel by git pre-push convention. The aggregator's
final report goes to stdout so it can be piped/captured separately from the
in-flight progress UI.
"""

from __future__ import annotations

import fnmatch
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, TextIO

from . import hook, reviewer
from .aggregate import Verdict
from .config import Config, ReviewerGate

# Width of the verdict column in the streaming line. Max of PASS/WARN/FAIL.
_VERDICT_W = 4

# Lock around stderr writes. Python's print is line-atomic under the GIL,
# but multiple workers writing multi-line content (e.g., retry notices from
# reviewer.py) can interleave without this.
_stream_lock = threading.Lock()


# --- public entry point -----------------------------------------------------

def review_all(
    payload: hook.ReviewPayload,
    config: Config,
    *,
    reviewer_fn: Callable[..., Verdict] = reviewer.review,
) -> list[Verdict]:
    spec = _read_spec(config)  # raises FileNotFoundError; hook.run catches it
    diff_text = hook.format_diff_payload(payload)

    active_personas, gate = _select_personas(payload, config)

    persona_jobs = [(name, _resolve_persona_path(name, config))
                    for name in active_personas]

    # Header first so the user has context before any synthetic verdicts.
    _stream_header(persona_jobs, payload, config, gate=gate)

    verdicts: list[Verdict] = []

    # Meta-WARN for oversized diffs. Not a "reviewer" — synthesized by the
    # hook itself, with agent_name = "claude-multi-agent-review".
    if payload.total_changed_lines > config.max_diff_lines:
        meta = _diff_size_meta_verdict(payload, config.max_diff_lines)
        _stream_verdict(meta)
        verdicts.append(meta)

    # Personas with no file resolve to synthetic verdicts immediately.
    real_jobs: list[tuple[str, Path]] = []
    for name, path in persona_jobs:
        if path is None:
            synth = _synthetic_missing_persona_verdict(
                name, config.treat_reviewer_failure_as
            )
            _stream_verdict(synth)
            verdicts.append(synth)
        else:
            real_jobs.append((name, path))

    if not real_jobs:
        return verdicts

    spinner = _Spinner([name for name, _ in real_jobs], stream=sys.stderr)
    spinner.start()
    try:
        if config.parallel and len(real_jobs) > 1:
            verdicts.extend(_dispatch_parallel(
                real_jobs, spec, diff_text, config, reviewer_fn, spinner,
            ))
        else:
            verdicts.extend(_dispatch_sequential(
                real_jobs, spec, diff_text, config, reviewer_fn, spinner,
            ))
    finally:
        spinner.stop()

    return verdicts


# --- dispatch ---------------------------------------------------------------

def _dispatch_parallel(
    jobs: list[tuple[str, Path]],
    spec: str,
    diff_text: str,
    config: Config,
    reviewer_fn: Callable[..., Verdict],
    spinner: _Spinner,
) -> list[Verdict]:
    verdicts: list[Verdict] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as exe:
        futures = {
            exe.submit(
                reviewer_fn,
                persona_name=name,
                persona_path=path,
                spec=spec,
                diff_payload=diff_text,
                config=config,
                log=_stream_line,
            ): name
            for name, path in jobs
        }
        for fut in as_completed(futures):
            try:
                v = fut.result()
            except Exception as e:  # reviewer.review shouldn't raise, but be safe
                name = futures[fut]
                v = _reviewer_crashed_verdict(name, e, config.treat_reviewer_failure_as)
            _stream_verdict(v, spinner=spinner)
            verdicts.append(v)
    return verdicts


def _dispatch_sequential(
    jobs: list[tuple[str, Path]],
    spec: str,
    diff_text: str,
    config: Config,
    reviewer_fn: Callable[..., Verdict],
    spinner: _Spinner,
) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for name, path in jobs:
        try:
            v = reviewer_fn(
                persona_name=name,
                persona_path=path,
                spec=spec,
                diff_payload=diff_text,
                config=config,
                log=_stream_line,
            )
        except Exception as e:  # reviewer.review shouldn't raise, but be safe
            v = _reviewer_crashed_verdict(name, e, config.treat_reviewer_failure_as)
        _stream_verdict(v, spinner=spinner)
        verdicts.append(v)
    return verdicts


# --- reviewer gate selection ------------------------------------------------

# Matches `diff --git a/<src> b/<dst>` headers. Quoted-form paths (used by
# git when a path needs C-style escaping) and unquoted paths are both
# captured. Unquoted paths can still contain literal spaces — git only
# quotes for control chars, quotes, backslashes, etc. The non-greedy
# `.+?` plus the `$` anchor lets the engine split unquoted "a/X b/Y" on
# the trailing ` b/` even when X or Y contain spaces.
_DIFF_GIT_LINE_RE = re.compile(
    r'^diff --git '
    r'(?:"a/(?P<aq>.+?)"|a/(?P<au>.+?))'
    r' '
    r'(?:"b/(?P<bq>.+?)"|b/(?P<bu>.+?))$',
    re.MULTILINE,
)


def _changed_files_from_payload(payload: hook.ReviewPayload) -> list[str]:
    """Extract unique changed file paths from every ref diff in the payload.

    Both sides of a rename are included so a gate covering either the old
    or new location applies. Paths are returned sorted for stable
    iteration in tests.
    """
    paths: set[str] = set()
    for review in payload.reviews:
        for m in _DIFF_GIT_LINE_RE.finditer(review.diff):
            a = m.group("aq") or m.group("au")
            b = m.group("bq") or m.group("bu")
            if a:
                paths.add(a)
            if b:
                paths.add(b)
    return sorted(paths)


def _select_personas(
    payload: hook.ReviewPayload,
    config: Config,
) -> tuple[list[str], ReviewerGate | None]:
    """Decide which personas to run based on `reviewer_gates`.

    A gate fires only when every changed file matches at least one of its
    patterns. The first matching gate wins; its `personas` list is
    intersected with `enabled_personas` (preserving enabled order) so a
    gate cannot resurrect a globally disabled persona. If no gate fires
    or no gates are configured, all enabled personas run.

    Returns (active personas, matched gate or None).
    """
    if not config.reviewer_gates:
        return list(config.enabled_personas), None

    changed = _changed_files_from_payload(payload)
    if not changed:
        # No files to match against — fall back to running everything
        # rather than guess at intent on an empty change set.
        return list(config.enabled_personas), None

    for gate in config.reviewer_gates:
        if not _gate_covers_all(gate, changed):
            continue
        gate_set = set(gate.personas)
        selected = [p for p in config.enabled_personas if p in gate_set]
        return selected, gate

    return list(config.enabled_personas), None


def _gate_covers_all(gate: ReviewerGate, files: list[str]) -> bool:
    """True iff every file matches at least one of the gate's patterns.

    Uses `fnmatch.fnmatchcase` for case-sensitive, OS-agnostic matching
    (the OS-aware `fnmatch.fnmatch` would normcase on Windows, which
    changes pattern semantics across platforms).
    """
    return all(
        any(fnmatch.fnmatchcase(f, p) for p in gate.patterns)
        for f in files
    )


# --- persona / spec resolution ----------------------------------------------

def _resolve_persona_path(name: str, config: Config) -> Path | None:
    """Repo-local override wins; shipped default is fallback. Returns None
    if neither exists — caller synthesizes a verdict for the user.

    Path-traversal safeguard: persona names come from user-controlled TOML
    config. A name like `../../etc/shadow` would otherwise have us read
    arbitrary files and pass them to `claude -p` as a system prompt.
    Resolve and verify the candidate stays under its expected root before
    accepting it. (CLAUDE.md security.path-traversal)
    """
    local_root = (config.repo_root / ".claude-multi-agent-review" / "personas").resolve()
    shipped_root = (config.install_root / "src" / "personas").resolve()

    local = (local_root / f"{name}.md").resolve()
    if _is_within(local, local_root) and local.is_file():
        return local
    shipped = (shipped_root / f"{name}.md").resolve()
    if _is_within(shipped, shipped_root) and shipped.is_file():
        return shipped
    return None


def _read_spec(config: Config) -> str:
    """Read the project spec from config.spec_path, resolved against
    repo_root. Path-traversal safeguard mirrors _resolve_persona_path:
    a config value like `../../etc/passwd` is rejected before we read."""
    repo_root_abs = config.repo_root.resolve()
    spec_path = (config.repo_root / config.spec_path).resolve()
    if not _is_within(spec_path, repo_root_abs):
        raise FileNotFoundError(
            f"spec_path {config.spec_path!r} resolves outside the repo "
            "root and is rejected as a path-traversal safeguard."
        )
    if not spec_path.is_file():
        raise FileNotFoundError(
            f"spec file not found at {spec_path}. Create CLAUDE.md at the "
            f"repo root, or set spec_path in .claude-multi-agent-review.toml."
        )
    return spec_path.read_text(encoding="utf-8")


def _is_within(path: Path, root: Path) -> bool:
    """True if `path` is `root` itself or a descendant of it. Caller is
    responsible for passing resolved (absolute) paths."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# --- synthetic verdicts -----------------------------------------------------

def _diff_size_meta_verdict(payload: hook.ReviewPayload, max_lines: int) -> Verdict:
    return Verdict(
        agent_name="claude-multi-agent-review",
        verdict="WARN",
        summary=(
            f"diff is large ({payload.total_changed_lines} lines > "
            f"max_diff_lines={max_lines}); reviewers may miss issues"
        ),
        reasoning=(
            f"This push contains {payload.total_changed_lines} changed lines, "
            f"which exceeds the configured max_diff_lines threshold of "
            f"{max_lines}. The full diff was passed through to every reviewer, "
            "but reviewers operate on a finite context window and may miss "
            "issues in regions they could not closely inspect. Consider "
            "splitting this work across multiple pushes, or raising "
            "max_diff_lines in .claude-multi-agent-review.toml if the threshold is "
            "too aggressive for your repo."
        ),
        findings=[],
    )


def _synthetic_missing_persona_verdict(name: str, mode: str) -> Verdict:
    level = "FAIL" if mode == "fail" else "WARN"
    return Verdict(
        agent_name=name,
        verdict=level,
        summary=f"persona file not found for {name!r}",
        reasoning=(
            f"The persona {name!r} is listed in enabled_personas but no "
            f"matching file was found at either:\n"
            f"  .claude-multi-agent-review/personas/{name}.md  (repo-local override)\n"
            f"  src/personas/{name}.md                  (shipped default)\n"
            "This is a configuration error. Fix enabled_personas in "
            ".claude-multi-agent-review.toml, or add the missing persona file."
        ),
        findings=[],
    )


def _reviewer_crashed_verdict(name: str, exc: BaseException, mode: str) -> Verdict:
    level = "FAIL" if mode == "fail" else "WARN"
    return Verdict(
        agent_name=name,
        verdict=level,
        summary=f"reviewer crashed: {type(exc).__name__}",
        reasoning=(
            f"The {name} reviewer raised an unexpected exception:\n"
            f"  {type(exc).__name__}: {exc}\n"
            "This is a bug in claude-multi-agent-review (reviewer.review should "
            "never raise for failure cases). Please report it."
        ),
        findings=[],
    )


# --- streaming UI -----------------------------------------------------------

def _stream_header(
    persona_jobs: list[tuple[str, Path | None]],
    payload: hook.ReviewPayload,
    config: Config,
    *,
    gate: ReviewerGate | None = None,
) -> None:
    with _stream_lock:
        ref_count = len(payload.reviews)
        line_count = payload.total_changed_lines
        n = len(persona_jobs)
        names = ", ".join(name for name, _ in persona_jobs)
        mode = "parallel" if config.parallel and n > 1 else "sequential"
        force = " [force-push]" if payload.has_force_push else ""
        print(
            f"claude-multi-agent-review: reviewing {ref_count} ref"
            f"{'s' if ref_count != 1 else ''}, "
            f"{line_count} changed lines{force} "
            f"with {n} reviewer{'s' if n != 1 else ''} "
            f"({mode}): {names}",
            file=sys.stderr,
            flush=True,
        )
        if gate is not None:
            total_enabled = len(config.enabled_personas)
            print(
                f"claude-multi-agent-review: gate {gate.name!r} matched; "
                f"running {n} of {total_enabled} reviewers ({names})",
                file=sys.stderr,
                flush=True,
            )
        elif config.reviewer_gates:
            print(
                "claude-multi-agent-review: no gate matched, running all "
                "enabled personas",
                file=sys.stderr,
                flush=True,
            )


def _stream_verdict(v: Verdict, spinner: "_Spinner | None" = None) -> None:
    with _stream_lock:
        if spinner is not None:
            spinner._clear_line_locked()
            spinner._remove_locked(v.agent_name)
        print(
            f"[{v.agent_name}] {v.verdict:<{_VERDICT_W}}  {v.summary}",
            file=sys.stderr,
            flush=True,
        )


def _stream_line(line: str) -> None:
    """Print one line to stderr under the stream lock.

    Passed as the `log` callback into reviewer.review() so retry-progress
    messages written from worker threads cannot race with the spinner's
    tick writes. Per CLAUDE.md output.streaming-via-lock, any code writing
    to stderr while the spinner is running MUST acquire _stream_lock.
    """
    with _stream_lock:
        print(line, file=sys.stderr, flush=True)


# --- spinner ----------------------------------------------------------------

class _Spinner:
    """Single aggregate spinner line at the bottom of the stream.

    Renders `  | reviewing: name1, name2` to stderr, refreshing on a timer.
    No-op when stderr isn't a TTY (CI, piped output): verdict lines still
    stream normally; the spinner just stays silent.

    Methods suffixed `_locked` assume the caller already holds
    `_stream_lock`. Public methods (`start`, `stop`) acquire it themselves.
    """

    _FRAMES = "|/-\\"
    _INTERVAL = 0.12  # seconds; ~8 Hz

    def __init__(self, names: list[str], *, stream: TextIO):
        self._in_progress = list(names)  # preserves enable order
        self._stream = stream
        self._enabled = stream.isatty()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_idx = 0
        self._line_drawn = False

    def start(self) -> None:
        if not self._enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        with _stream_lock:
            self._clear_line_locked()

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            self._tick()
            # Wait on the stop event instead of time.sleep so stop() returns
            # promptly when called mid-interval.
            self._stop_evt.wait(self._INTERVAL)

    def _tick(self) -> None:
        with _stream_lock:
            if not self._in_progress or self._stop_evt.is_set():
                self._clear_line_locked()
                return
            frame = self._FRAMES[self._frame_idx % len(self._FRAMES)]
            self._frame_idx += 1
            names = ", ".join(self._in_progress)
            # \r to return to line start, ANSI clear-to-end-of-line, then
            # the spinner content. Leaves the cursor at end of the line.
            self._stream.write(f"\r\x1b[K  {frame} reviewing: {names}")
            self._stream.flush()
            self._line_drawn = True

    # --- locked helpers (caller must hold _stream_lock) ---------------------

    def _clear_line_locked(self) -> None:
        if self._line_drawn:
            self._stream.write("\r\x1b[K")
            self._stream.flush()
            self._line_drawn = False

    def _remove_locked(self, name: str) -> None:
        try:
            self._in_progress.remove(name)
        except ValueError:
            pass  # already removed (defensive)
