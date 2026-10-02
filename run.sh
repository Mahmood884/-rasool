#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
if [[ ! -d .venv ]]; then
    echo "No .venv found. Run ./install.sh first." >&2
    exit 1
fi
exec .venv/bin/python main.py "$@"
