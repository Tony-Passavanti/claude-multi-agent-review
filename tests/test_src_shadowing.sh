#!/usr/bin/env sh
# test_src_shadowing.sh
#
# Integration test for the install fix from PR #8 (commit 7026000).
#
# Scenario: a consuming repo has its own top-level `src/` package
# (the common "src layout" — extremely widespread in Python projects).
# When git fires the pre-push hook, cwd is the consuming repo. Without
# the `python -P` flag, cwd would be on sys.path ahead of PYTHONPATH,
# and `python -m src` would resolve the consuming repo's `src/` instead
# of ours — typically crashing with `No module named src.__main__`
# (best case) or silently running the wrong code (worst case).
#
# This test reproduces the exact scenario from a tmp directory, invokes
# the shim with empty pre-push stdin, and asserts a clean exit. If
# someone "simplifies" the shim and accidentally removes either the
# `-P` flag or the PYTHONPATH=$INSTALL_ROOT line, this regression test
# catches it.

set -eu

# Resolve INSTALL_ROOT relative to this script's location.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SHIM="$INSTALL_ROOT/bin/claude-multi-agent-review"

if [ ! -f "$SHIM" ]; then
  echo "FAIL: shim not found at $SHIM" >&2
  exit 1
fi

# Create a temp dir with a conflicting `src/` package. This is what a
# consuming repo using src-layout looks like.
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR/src"
echo "# decoy src package (would shadow ours without -P + PYTHONPATH)" \
  > "$TMP_DIR/src/__init__.py"

# Invoke the shim from the consuming repo's cwd. Empty stdin (no refs
# being pushed) → the hook should short-circuit on empty payload and
# exit 0.
cd "$TMP_DIR"
output="$(echo "" | "$SHIM" --install-root "$INSTALL_ROOT" \
  origin "fake-remote-url" 2>&1)"
rc=$?

if [ "$rc" -ne 0 ]; then
  echo "FAIL: shim exit code $rc (expected 0)" >&2
  echo "--- output ---" >&2
  echo "$output" >&2
  echo "--- end output ---" >&2
  exit 1
fi

echo "PASS: shim correctly resolved src from install root despite conflict"
exit 0
