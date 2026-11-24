#!/usr/bin/env bash
# Start the Dot controller.
#
# DOT_PYTHON pins the interpreter: a venv activated in your shell is not
# necessarily the one the supervisor inherits, and a controller that dies on
# "No module named zeroconf" reads like the device dropped off the network.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${DOT_PYTHON:-$(command -v python3 || command -v python)}"
exec "$PY" -u controller.py
