#!/bin/bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec "${PYTHON_BIN:-python3}" "$SCRIPT_DIR/server.py" "$@"
