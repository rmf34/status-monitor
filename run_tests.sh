#!/usr/bin/env bash
# Run the test suite. Args are forwarded to pytest.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m pytest tests/ -v "$@"
