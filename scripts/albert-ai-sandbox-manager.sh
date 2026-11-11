#!/bin/bash
# ALBERT | AI Sandbox manager CLI.
#
# Thin wrapper around the Python implementation (scripts/albert_sandbox/cli.py).
# Commands and JSON output are compatible with the former Bash manager:
#   create [name] | remove <name> | start | stop | restart | status [name] | list | build
# plus: resources, persistent, client, admin, reconcile, migrate, mount-disks, selfcheck.
# Run with "help" for details.

INSTALL_DIR="${ALBERT_INSTALL_DIR:-/opt/albert-ai-sandbox-manager}"
SCRIPT_DIR="$( cd "$( dirname "$(readlink -f "${BASH_SOURCE[0]}")" )" && pwd )"

PYTHON="${INSTALL_DIR}/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:$PYTHONPATH}"
# Compat: the former Bash manager read ALBERT_JSON=1 to switch to JSON output.
if [ -n "${ALBERT_JSON:-}" ]; then
	set -- "$@" --json
fi
exec "$PYTHON" -m albert_sandbox.cli "$@"
