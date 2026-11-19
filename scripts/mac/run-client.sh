#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

args=(--no-gui --no-mic)
if [ $# -ge 1 ]; then
    args+=(--ip "$1")
fi

exec uv run python -m soundbridge client "${args[@]}"
