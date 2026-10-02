
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo ">>> Creating virtualenv with uv + Python 3.12"
uv venv --python 3.12 .venv

echo ">>> Installing rasool (core)"
uv pip install --python .venv/bin/python -e .

echo ">>> Installing optional extras (sniff only for now)"
uv pip install --python .venv/bin/python -e '.[sniff]' || {
    echo ">>> scapy install failed. Continuing with core only." >&2
}

echo ">>> Done. Run ./run.sh to start."
