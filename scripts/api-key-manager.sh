#!/bin/bash
# Compatibility wrapper for API key (client) management.
#
#   api-key-manager.sh create --label "My Agent"   -> albert-ai-sandbox-manager client create --label ...
#   api-key-manager.sh list                         -> albert-ai-sandbox-manager client list
#   api-key-manager.sh revoke --key <KEY> | <KEY>   -> albert-ai-sandbox-manager client delete --key ...
#
# Revoking a key removes all sandboxes of that client including their data.
# Prefer the admin web UI (/manager/admin/) or "albert-ai-sandbox-manager client ..." for quotas.

SCRIPT_DIR="$( cd "$( dirname "$(readlink -f "${BASH_SOURCE[0]}")" )" && pwd )"
MANAGER="${SCRIPT_DIR}/albert-ai-sandbox-manager.sh"

cmd="${1:-}"
shift || true
case "$cmd" in
	create)
		exec "$MANAGER" client create "$@" ;;
	list)
		exec "$MANAGER" client list "$@" ;;
	revoke)
		if [ "${1:-}" = "--key" ]; then
			exec "$MANAGER" client delete --key "${2:-}"
		elif [ "${1:-}" = "--" ]; then
			exec "$MANAGER" client delete --key "${2:-}"
		else
			exec "$MANAGER" client delete --key "${1:-}"
		fi ;;
	*)
		echo "Usage: $0 {create --label <LABEL> | list | revoke --key <KEY>}" >&2
		exit 2 ;;
esac
