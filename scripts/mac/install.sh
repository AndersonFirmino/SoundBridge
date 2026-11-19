#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [ "$(uname)" != "Darwin" ]; then
    echo "This script is for macOS (Darwin) only." >&2
    exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found. Install it from https://brew.sh (needs the administrator password of the user), then run this script again." >&2
    exit 2
fi

if ! brew list opus >/dev/null 2>&1; then
    echo "==> brew install opus"
    brew install opus
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "==> brew install uv"
    brew install uv
fi

echo "==> uv sync"
uv sync

echo "==> checking libopus"
if ! uv run python -c "from soundbridge.opus import _load_libopus; print(_load_libopus())"; then
    echo "libopus could not be loaded. Contents of $(brew --prefix opus)/lib:" >&2
    ls -la "$(brew --prefix opus)/lib" >&2 || true
    exit 1
fi

echo "==> audio devices"
uv run python -m soundbridge client --list-devices

echo
echo "Done. Next: scripts/mac/run-client.sh <WINDOWS_SERVER_IP>"
