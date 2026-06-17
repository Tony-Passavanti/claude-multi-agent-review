"""Pre-push hook orchestrator.

Two layers live in this file:

1. The stdin/diff layer (parse_stdin, build_payload, and the helpers they
   call) is pure git plumbing — it turns the four-tuple lines git feeds us
   into one aggregated ReviewPayload describing what's being published.
2. The top-level `run()` wires that payload into the reviewer fan-out.
   Orchestration itself lives in `orchestrate.review_all`; this module
   loads config, parses stdin, builds the payload, and delegates.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

# Git's well-known empty tree SHA. Used as the diff base when a brand-new
# branch is pushed to a remote with no resolvable default branch (e.g. the
# very first push to an empty repo). Lets us still produce a diff covering
# the entire history of the new branch.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_ZERO_SHA_RE = re.compile(r"^0+$")


def is_zero_sha(sha: str) -> bool:
    """True if `sha` is git's all-zeros placeholder. Length-agnostic so
    SHA-256 repos work without changes."""
    return bool(sha) and _ZERO_SHA_RE.match(sha) is not None


@dataclass(frozen=True)
class RefUpdate:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    @property
    def is_delete(self) -> bool:
        return is_zero_sha(self.local_sha)

    @property
    def is_new_branch(self) -> bool:
        return is_zero_sha(self.remote_sha) and not self.is_tag

    @property
    def is_tag(self) -> bool:
        return self.remote_ref.startswith("refs/tags/")


@dataclass(frozen=True)
class RefReview:
    """One ref's contribution to the review payload."""
    ref: RefUpdate
    base_sha: str
    base_label: str        # human-readable, e.g. "origin/main" or "remote (force-push base)"
    head_sha: str
    is_force_push: bool
    commit_log: str        # `git log --oneline <base>..<head>`
    diff: str              # `git diff <base> <head>`
    changed_lines: int
    # Canonical list of changed file paths from `git diff --name-only -z`.
    # Defaulted so older positional callers (tests, third-party) keep
    # working, and so the public dataclass signature stays additive
    # (api.public-symbols). Consumers that need to match paths (e.g.
    # reviewer gates) MUST use this rather than parsing `diff` — diff-
    # header parsing is ambiguous for paths containing literal ` b/`.
    changed_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewPayload:
    reviews: list[RefReview] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (ref, reason)

    @property
    def is_empty(self) -> bool:
        return not self.reviews

    @property
    def total_changed_lines(self) -> int:
        return sum(r.changed_lines for r in self.reviews)

    @property
    def has_force_push(self) -> bool:
        return any(r.is_force_push for r in self.reviews)


# --- stdin parsing ----------------------------------------------------------

def parse_stdin(stdin: TextIO) -> list[RefUpdate]:
    """Parse the pre-push stdin format. Blank lines are ignored; malformed
    lines raise ValueError (the hook itself is broken if git gives us junk)."""
    refs: list[RefUpdate] = []
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 4:
            raise ValueError(f"unexpected pre-push line: {line!r}")
        refs.append(RefUpdate(*parts))
    return refs


# --- git helpers ------------------------------------------------------------

def _git(*args: str, repo_root: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=check,
    )


def _rev_parse(rev: str, repo_root: Path) -> str | None:
    proc = _git("rev-parse", "--verify", "--quiet", rev, repo_root=repo_root, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def resolve_default_branch(
    *,
    repo_root: Path,
    remote_name: str,
    config_override: str,
) -> tuple[str, str] | None:
    """Return (rev, label) for the default branch's tip, or None if none
    can be resolved. `config_override` wins when non-empty. Otherwise try
    `<remote>/HEAD`, then `<remote>/main`, then `<remote>/master`.
    """
    if config_override:
        rev = _rev_parse(config_override, repo_root)
        if rev:
            return rev, config_override
        return None

    candidates = [
        f"refs/remotes/{remote_name}/HEAD",
        f"refs/remotes/{remote_name}/main",
        f"refs/remotes/{remote_name}/master",
    ]
    for cand in candidates:
        rev = _rev_parse(cand, repo_root)
        if rev:
            # Prettier label for HEAD: resolve to its symbolic target if we can.
            if cand.endswith("/HEAD"):
                sym = _git("symbolic-ref", "--quiet", cand, repo_root=repo_root, check=False)
                if sym.returncode == 0 and sym.stdout.strip():
                    target = sym.stdout.strip().removeprefix("refs/remotes/")
                    return rev, target
            return rev, cand.removeprefix("refs/remotes/")
    return None


def _is_force_push(ref: RefUpdate, repo_root: Path) -> bool:
    if ref.is_new_branch or ref.is_delete:
        return False
    proc = _git(
        "merge-base", "--is-ancestor", ref.remote_sha, ref.local_sha,
        repo_root=repo_root, check=False,
    )
    # Exit 0 => ancestor (fast-forward); 1 => not ancestor (force-push).
    # Other exit codes => something weird; treat as non-force to avoid
    # false alarms from transient git errors.
    return proc.returncode == 1


def _diff_stats_changed_lines(base: str, head: str, repo_root: Path) -> int:
    """Cheap line-change count from `git diff --shortstat`."""
    proc = _git("diff", "--shortstat", f"{base}..{head}", repo_root=repo_root, check=False)
    if proc.returncode != 0:
        return 0
    # e.g. " 4 files changed, 120 insertions(+), 8 deletions(-)"
    total = 0
    for m in re.finditer(r"(\d+) (insertion|deletion)", proc.stdout):
        total += int(m.group(1))
    return total


def _diff_changed_files(base: str, head: str, repo_root: Path) -> list[str]:
    """Unambiguous list of changed paths via `git diff --name-only -z`.

    Uses NUL termination so paths with spaces, embedded ` b/`, or other
    characters that would render diff-header parsing ambiguous round-trip
    safely. Empty / missing output → empty list (treat as no changes).

    `--no-renames` disables rename detection so a rename appears as
    delete-old + add-new (both paths emitted) rather than only the
    destination. This matches the old regex parser's behavior of
    capturing both sides of a `diff --git a/<src> b/<dst>` rename
    header, and keeps gates that pattern-match on the source path
    firing correctly when a file is moved into or out of a guarded
    directory.
    """
    proc = _git(
        "diff", "--name-only", "--no-renames", "-z",
        f"{base}..{head}",
        repo_root=repo_root, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return []
    # `-z` outputs each path followed by NUL; trailing NUL after the last
    # path is normal, so strip it before split to avoid an empty tail.
    return [p for p in proc.stdout.rstrip("\x00").split("\x00") if p]


# --- per-ref review construction --------------------------------------------

def _build_ref_review(
    ref: RefUpdate,
    *,
    repo_root: Path,
    remote_name: str,
    default_branch_override: str,
) -> RefReview | tuple[None, str]:
    """Either return a RefReview, or (None, reason) to record in `skipped`."""
    if ref.is_delete:
        return None, "branch deletion"

    is_force = _is_force_push(ref, repo_root)

    if ref.is_new_branch:
        default = resolve_default_branch(
            repo_root=repo_root,
            remote_name=remote_name,
            config_override=default_branch_override,
        )
        if default is not None:
            default_sha, default_label = default
            mb = _git(
                "merge-base", ref.local_sha, default_sha,
                repo_root=repo_root, check=False,
            )
            if mb.returncode == 0 and mb.stdout.strip():
                base_sha = mb.stdout.strip()
                base_label = f"merge-base with {default_label}"
            else:
                base_sha = EMPTY_TREE_SHA
                base_label = "empty tree (no merge-base with default branch)"
        else:
            base_sha = EMPTY_TREE_SHA
            base_label = "empty tree (no default branch resolvable)"
    else:
        base_sha = ref.remote_sha
        base_label = f"{remote_name} ({ref.remote_ref})"
        if is_force:
            base_label += " [force-push base]"

    diff_proc = _git(
        "diff", f"{base_sha}..{ref.local_sha}",
        repo_root=repo_root, check=False,
    )
    if diff_proc.returncode != 0:
        return None, f"git diff failed: {diff_proc.stderr.strip()}"

    diff_text = diff_proc.stdout
    if not diff_text.strip():
        return None, "empty diff"

    log_proc = _git(
        "log", "--oneline", "--no-decorate", f"{base_sha}..{ref.local_sha}",
        repo_root=repo_root, check=False,
    )
    commit_log = log_proc.stdout if log_proc.returncode == 0 else ""

    changed_lines = _diff_stats_changed_lines(base_sha, ref.local_sha, repo_root)
    changed_files = _diff_changed_files(base_sha, ref.local_sha, repo_root)

    return RefReview(
        ref=ref,
        base_sha=base_sha,
        base_label=base_label,
        head_sha=ref.local_sha,
        is_force_push=is_force,
        commit_log=commit_log,
        diff=diff_text,
        changed_lines=changed_lines,
        changed_files=changed_files,
    )


def build_payload(
    refs: list[RefUpdate],
    *,
    repo_root: Path,
    remote_name: str,
    review_tags: bool,
    default_branch_override: str,
) -> ReviewPayload:
    reviews: list[RefReview] = []
    skipped: list[tuple[str, str]] = []

    for ref in refs:
        if ref.is_tag and not review_tags:
            skipped.append((ref.remote_ref, "tag (review_tags disabled)"))
            continue

        result = _build_ref_review(
            ref,
            repo_root=repo_root,
            remote_name=remote_name,
            default_branch_override=default_branch_override,
        )
        if isinstance(result, RefReview):
            reviews.append(result)
        else:
            _, reason = result
            skipped.append((ref.remote_ref, reason))

    return ReviewPayload(reviews=reviews, skipped=skipped)


# --- payload text formatting ------------------------------------------------

def format_diff_payload(payload: ReviewPayload) -> str:
    """Render a ReviewPayload into the text fed to each reviewer's stdin.

    Multi-ref pushes are concatenated with clear `=== ref: ... ===` headers
    so a reviewer can attribute findings to the right ref. Skipped refs are
    reported at the end as a visible footnote — useful when a push mixed
    deletes/tags with reviewable refs.
    """
    parts: list[str] = []
    for review in payload.reviews:
        parts.append(_format_one_ref(review))
    if payload.skipped:
        parts.append(_format_skipped(payload.skipped))
    return "\n\n".join(parts)


def _format_one_ref(review: RefReview) -> str:
    flags: list[str] = []
    if review.ref.is_new_branch:
        flags.append("new branch")
    if review.is_force_push:
        flags.append("force-push")
    flag_str = f" [{', '.join(flags)}]" if flags else ""

    lines: list[str] = [
        f"=== ref: {review.ref.remote_ref}{flag_str} ===",
        f"base:  {review.base_label} ({review.base_sha[:12]})",
        f"head:  {review.head_sha[:12]}",
        f"changed lines: {review.changed_lines}",
    ]

    log_text = review.commit_log.strip()
    if log_text:
        log_lines = log_text.splitlines()
        lines.append(f"commits ({len(log_lines)}):")
        lines.extend(f"  {line}" for line in log_lines)

    if review.is_force_push:
        lines.append("note: this ref rewrites history on the remote.")

    lines.append("")
    lines.append("--- diff ---")
    lines.append(review.diff.rstrip())
    return "\n".join(lines)


def _format_skipped(skipped: list[tuple[str, str]]) -> str:
    out = ["=== skipped refs ==="]
    out.extend(f"  {ref}: {reason}" for ref, reason in skipped)
    return "\n".join(out)


# --- top-level orchestrator -------------------------------------------------

def run(
    *,
    install_root: Path,
    repo_root: Path,
    remote_name: str,
    remote_url: str,
    stdin: TextIO,
) -> int:
    # Imported here to avoid a circular import at module load.
    from . import aggregate, config as config_mod, metrics, orchestrate

    # Load config first so `override_env` is repo-configurable. Cost is
    # two TOML reads; negligible compared to a `claude -p` invocation.
    try:
        config = config_mod.load(install_root=install_root, repo_root=repo_root)
    except (FileNotFoundError, ValueError) as e:
        print(f"claude-multi-agent-review: config error: {e}", file=sys.stderr)
        print(
            "Hook exiting with code 2; push allowed to avoid lockout.",
            file=sys.stderr,
        )
        return 2

    # Bypass env: drain stdin best-effort so git doesn't see a SIGPIPE,
    # then exit cleanly. Narrow to OSError (broken pipe, closed fd, etc.)
    # per CLAUDE.md errors.no-bare-except, which forbids silent broad-
    # catch swallows in src/.
    if os.environ.get(config.override_env) == "1":
        try:
            stdin.read()
        except OSError:
            pass
        print(
            f"claude-multi-agent-review: bypassed ({config.override_env}=1)",
            file=sys.stderr,
        )
        return 0

    try:
        refs = parse_stdin(stdin)
    except ValueError as e:
        # Spec rule errors.hook-internal-exit-code-2: malformed stdin is a
        # hook-internal failure (the hook can't do its job), not a reviewer
        # FAIL. Exit 2 so callers don't conflate the two.
        print(
            f"claude-multi-agent-review: malformed pre-push stdin: {e}",
            file=sys.stderr,
        )
        print(
            "Hook exiting with code 2; push allowed to avoid lockout.",
            file=sys.stderr,
        )
        return 2
    if not refs:
        return 0  # nothing to do

    payload = build_payload(
        refs,
        repo_root=repo_root,
        remote_name=remote_name or "origin",
        review_tags=config.review_tags,
        default_branch_override=config.default_branch,
    )

    if payload.is_empty:
        print("claude-multi-agent-review: nothing to review (all refs skipped):",
              file=sys.stderr)
        for ref, reason in payload.skipped:
            print(f"  {ref}: {reason}", file=sys.stderr)
        return 0

    # Collect per-reviewer usage only when metrics are enabled, so disabling
    # them skips the work entirely. orchestrate serializes appends to this
    # list under its stream lock, so reading it after review_all returns
    # (all reviewer threads joined) is safe.
    usages: list[metrics.ReviewerUsage] = []
    usage_sink = usages.append if config.metrics_enabled else None

    try:
        verdicts = orchestrate.review_all(payload, config, usage_sink=usage_sink)
    except FileNotFoundError as e:
        print(f"claude-multi-agent-review: {e}", file=sys.stderr)
        print(
            "Hook exiting with code 2; push allowed to avoid lockout.",
            file=sys.stderr,
        )
        return 2

    exit_code, report = aggregate.aggregate(verdicts)
    aggregate.print_report(report)

    # Per-run metrics: best-effort, never alters exit_code. The spinner has
    # stopped by now, so the summary can go straight to stderr.
    if config.metrics_enabled:
        summary = metrics.record_run(
            config, verdicts, usages,
            changed_lines=payload.total_changed_lines,
            exit_code=exit_code,
        )
        print(summary, file=sys.stderr, flush=True)

    return exit_code
