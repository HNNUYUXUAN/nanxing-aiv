#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python scripts/reproduce.py "$@"
fi
exec python3 scripts/reproduce.py "$@"
