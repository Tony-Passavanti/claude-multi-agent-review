"""Entry point for `python -m src`.

Invoked by bin/claude-multi-agent-review with the pre-push hook's argv and stdin.
Argv layout (after our own --install-root flag):
    $1  remote name (e.g. "origin")
    $2  remote URL  (e.g. "git@github.com:owner/repo.git")
Stdin: zero or more lines of `<local-ref> <local-sha> <remote-ref> <remote-sha>`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import hook


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 on the output streams. Windows defaults sys.stdout /
    # sys.stderr to cp1252, which can't encode characters that legitimately
    # appear in the aggregated report and reviewer reasoning text (e.g.
    # `→` from CLAUDE.md rule citations). Done here rather than at module
    # import so library importers (tests, dev tools) don't get global
    # stdio mutated as a side effect.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        # reconfigure() is unavailable on non-TextIOWrapper streams (some
        # test capture / embedded-interpreter setups). Best-effort.
        pass

    try:
        parser = argparse.ArgumentParser(
            prog="claude-multi-agent-review", add_help=False,
        )
        parser.add_argument("--install-root", required=True, type=Path)
        parser.add_argument("remote_name", nargs="?", default="")
        parser.add_argument("remote_url", nargs="?", default="")
        args = parser.parse_args(argv)
        return hook.run(
            install_root=args.install_root,
            repo_root=Path.cwd(),
            remote_name=args.remote_name,
            remote_url=args.remote_url,
            stdin=sys.stdin,
        )
    except Exception as e:
        # Top-level safety net per CLAUDE.md errors.hook-internal-exit-code-2:
        # any uncaught exception is a hook-internal failure (not a reviewer
        # FAIL), so produce exit code 2 with a diagnostic. Letting a raw
        # exception propagate would produce exit code 1, which is reserved
        # for "push blocked by reviewer FAIL" and would be misleading.
        # SystemExit and KeyboardInterrupt deliberately not caught — they
        # propagate naturally so argparse --help and Ctrl-C behave normally.
        print(
            f"claude-multi-agent-review: hook crashed with "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        print(
            "Hook exiting with code 2; push allowed to avoid lockout.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
