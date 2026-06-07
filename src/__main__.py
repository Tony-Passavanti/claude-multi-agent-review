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
    parser = argparse.ArgumentParser(prog="claude-multi-agent-review", add_help=False)
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


if __name__ == "__main__":
    sys.exit(main())
