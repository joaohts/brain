#!/bin/bash
# Entry point for the brain service (deploy/systemd/brain.service.in).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec .venv/bin/python -u -m brain.server
