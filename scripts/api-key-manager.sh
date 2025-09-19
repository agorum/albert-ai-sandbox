#!/bin/bash
# Wrapper script for API key management (uses Python CLI api_key_manager.py)
# Location assumption: installed under /opt/albert-ai-sandbox-manager

SCRIPT_DIR="/opt/albert-ai-sandbox-manager/scripts"
PY_SCRIPT="${SCRIPT_DIR}/api_key_manager.py"

if [ ! -f "$PY_SCRIPT" ]; then
  echo "Python API key manager script not found at $PY_SCRIPT" >&2
  exit 1
fi

show_help() {
  echo "ALBERT | API Key Manager"
  echo "Usage: $0 <command> [options]"
  echo ""
  echo "Commands:"
  echo "  create [--label <text>]    Create a new API key (prints key once)"
  echo "  list                       List existing API keys (hash prefixes)"
  echo "  revoke --key <PLAINTEXT>   Revoke a key (removes its containers & data)"
  echo "  help                       Show this help"
  echo ""
  echo "Environment variables:"
  echo "  MANAGER_DB_PATH     Path to SQLite DB (default ./data/manager.db)"
  echo "  MANAGER_DATA_DIR    Data directory root (default ./data/containers)"
}

CMD="$1"
shift || true

case "$CMD" in
  create)
    # pass through label if provided
    LABEL=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --label)
          LABEL="$2"; shift 2;;
        *) echo "Unknown option: $1"; exit 2;;
      esac
    done
    if [ -n "$LABEL" ]; then
      python3 "$PY_SCRIPT" create --label "$LABEL"
    else
      python3 "$PY_SCRIPT" create
    fi
    ;;
  list)
    python3 "$PY_SCRIPT" list
    ;;
  revoke)
    KEY=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --key)
          KEY="$2"; shift 2;;
        *) echo "Unknown option: $1"; exit 2;;
      esac
    done
    if [ -z "$KEY" ]; then
      echo "--key <PLAINTEXT_KEY> is required" >&2
      exit 2
    fi
    python3 "$PY_SCRIPT" revoke --key "$KEY"
    ;;
  help|--help|-h|"")
    show_help
    ;;
  *)
    echo "Unknown command: $CMD" >&2
    show_help
    exit 1
    ;;
 esac
