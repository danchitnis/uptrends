#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_dir"

bootstrap_python="${PYTHON:-python3}"
"$bootstrap_python" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("uP Trend Chart requires Python 3.11 or newer")
PY

if [[ ! -x .venv/bin/python ]]; then
    "$bootstrap_python" -m venv .venv
fi
.venv/bin/python - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Existing .venv uses Python older than 3.11; recreate it")
PY

export MPLCONFIGDIR="$repo_dir/.venv/matplotlib"
export XDG_CACHE_HOME="$repo_dir/.venv/cache"
export PIP_CACHE_DIR="$repo_dir/.venv/cache/pip"
mkdir -p "$MPLCONFIGDIR" "$XDG_CACHE_HOME" "$PIP_CACHE_DIR"

.venv/bin/python -m pip install --quiet -r requirements.txt
.venv/bin/python generate_uptrend_legacy.py
.venv/bin/python generate_uptrend.py
