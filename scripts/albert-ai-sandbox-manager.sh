#!/bin/bash

source /opt/albert-ai-sandbox-manager/scripts/common.sh
source /opt/albert-ai-sandbox-manager/scripts/port-manager.sh
source /opt/albert-ai-sandbox-manager/scripts/nginx-manager.sh

# IMPORTANT: A sourced script (nginx-manager.sh) previously enabled 'set -euo pipefail'.
# These strict modes were causing silent premature exits before JSON/trace output.
# We explicitly relax them here and implement our own explicit error handling so that
# json_error / trace instrumentation always has a chance to run.
set +e 2>/dev/null || true
set +u 2>/dev/null || true
set +o pipefail 2>/dev/null || true

# Early trace (before any heavy logic) – helps confirm script actually starts.
if [ -n "${ALBERT_TRACE:-}" ]; then
	echo "[TRACE] manager start pid=$$ PWD=$(pwd) args:$*" >&2
	# Show current critical shell option states for debugging
	(set -o | grep -E 'errexit|nounset|pipefail' || true) >&2
fi

DOCKER_IMAGE="albert-ai-sandbox:latest"

# DB path (must match manager service). Allow override via MANAGER_DB_PATH.
# Safe for 'set -u' (nounset) shells using ${VAR:-} expansions.
resolve_db_path() {
	local override="${MANAGER_DB_PATH:-}"
	if [ -n "$override" ]; then
		DB_PATH="$override"
		return
	fi
	if [ -f "/opt/albert-ai-sandbox-manager/data/manager.db" ]; then
		DB_PATH="/opt/albert-ai-sandbox-manager/data/manager.db"
	elif [ -f "$(pwd)/data/manager.db" ]; then
		DB_PATH="$(pwd)/data/manager.db"
	else
		DB_PATH="/opt/albert-ai-sandbox-manager/data/manager.db"  # will be created on first key insert
	fi
}
resolve_db_path
export DB_PATH  # ensure python heredocs can read it

# Extended modes
JSON_MODE="${ALBERT_JSON:-}"          # set to any non-empty for JSON output
OWNER_KEY_HASH_ENV="${ALBERT_OWNER_KEY_HASH:-}"  # passed in by REST service
NON_INTERACTIVE="${ALBERT_NONINTERACTIVE:-}"     # suppress prompts
DEBUG=""
QUIET=""
TRACE="${ALBERT_TRACE:-}"
STATUS_SKIP_STATS="${ALBERT_STATUS_SKIP_STATS:-}"
MCPHUB_WAIT_ERROR=""

# Parse optional global flags (support both before and after command)
ORIG_ARGS=("$@")
FIRST_PASS=()
COMMAND_SEEN=""
while [[ $# -gt 0 ]]; do
	case "$1" in
		--json) JSON_MODE=1; shift ;;
		--quiet) QUIET=1; shift ;;
		--api-key-hash)
			[ -z "$2" ] && { echo "Missing value for --api-key-hash" >&2; exit 2; }
			OWNER_KEY_HASH_ENV="$2"; shift 2 ;;
		--api-key)
			[ -z "$2" ] && { echo "Missing value for --api-key" >&2; exit 2; }
			if command -v python3 >/dev/null 2>&1; then
				OWNER_KEY_HASH_ENV=$(python3 -c 'import sys,hashlib;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$2")
			else
				OWNER_KEY_HASH_ENV=$(printf "%s" "$2" | openssl dgst -sha256 | awk '{print $2}')
			fi
			shift 2 ;;
		--non-interactive) NON_INTERACTIVE=1; shift ;;
		--debug) DEBUG=1; shift ;;
		--)
			shift; while [[ $# -gt 0 ]]; do FIRST_PASS+=("$1"); shift; done; break ;;
		create|remove|delete|start|stop|restart|status|list|build|help|--help|-h)
			COMMAND_SEEN="$1"
			FIRST_PASS+=("$1")
			shift
			# Collect rest for second pass (may contain flags)
			while [[ $# -gt 0 ]]; do FIRST_PASS+=("$1"); shift; done
			break ;;
		"") break ;;
		*) FIRST_PASS+=("$1"); shift ;;
	esac
done
# Expose early debug after first pass
debug_log() { [ -n "$DEBUG" ] && echo -e "${YELLOW}[DEBUG] $*${NC}" >&2; }
trace_log() { [ -n "$TRACE" ] && echo "[TRACE] $*" >&2; }

# Track whether any JSON has been emitted (for debugging silent failures)
__ALBERT_JSON_EMITTED=""

# Global EXIT trap to catch silent early termination and emit JSON diagnostics.
_albert_exit_trap() {
	local rc=$?
	if [ -n "${ALBERT_TRACE:-}" ]; then
		echo "[TRACE] global exit trap rc=$rc JSON_MODE='${JSON_MODE}' emitted='${__ALBERT_JSON_EMITTED}'" >&2
	fi
	if [ -n "$JSON_MODE" ] && [ -z "$__ALBERT_JSON_EMITTED" ]; then
		# Avoid recursive trap if json_error triggered exit intentionally (it sets emitted flag already)
		printf '{"error":"internal","message":"Exited prematurely (trap)","exitCode":%s}\n' "$rc" >&2
	fi
}
trap _albert_exit_trap EXIT

# Emit JSON safely (expects complete object spec via jq args)
json_emit() {
	# Usage: json_emit '{result:"ok"}' OR with args passed
	if [ -n "$JSON_MODE" ]; then
		if [ $# -eq 1 ]; then
			# Single raw jq program
			jq -n "$1" && __ALBERT_JSON_EMITTED=1
		else
			# Program followed by --arg pairs
			local program="$1"; shift
			jq -n "$program" "$@" && __ALBERT_JSON_EMITTED=1
		fi
	fi
}

if [ ${#FIRST_PASS[@]} -eq 0 ]; then
	set -- "${ORIG_ARGS[@]}"
else
	set -- "${FIRST_PASS[@]}"
fi

# Fallback detection: if --json present in original args but JSON_MODE not set (parsing edge case)
if [ -z "$JSON_MODE" ]; then
	for arg in "${ORIG_ARGS[@]}"; do
		if [ "$arg" = "--json" ]; then JSON_MODE=1; break; fi
	done
fi

# Second pass: if command present, allow flags after it
if [[ -n "$COMMAND_SEEN" ]]; then
  CMD="$1"; shift || true
  POST_FLAGS=()
      while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) JSON_MODE=1; shift ;;
      --quiet) QUIET=1; shift ;;
      --api-key-hash)
        [ -z "$2" ] && { echo "Missing value for --api-key-hash" >&2; exit 2; }
        OWNER_KEY_HASH_ENV="$2"; shift 2 ;;
      --api-key)
        [ -z "$2" ] && { echo "Missing value for --api-key" >&2; exit 2; }
        if command -v python3 >/dev/null 2>&1; then
          OWNER_KEY_HASH_ENV=$(python3 -c 'import sys,hashlib;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$2")
        else
          OWNER_KEY_HASH_ENV=$(printf "%s" "$2" | openssl dgst -sha256 | awk '{print $2}')
        fi
        shift 2 ;;
      --non-interactive) NON_INTERACTIVE=1; shift ;;
      --debug) DEBUG=1; shift ;;
      *) POST_FLAGS+=("$1"); shift ;;
    esac
  done
  set -- "$CMD" "${POST_FLAGS[@]}"
fi

# Early debug snapshot
if [ -n "$DEBUG" ]; then
  echo -e "${YELLOW}[DEBUG] Effective command: $*${NC}" >&2
  echo -e "${YELLOW}[DEBUG] DB_PATH(initial)=${DB_PATH}${NC}" >&2
  echo -e "${YELLOW}[DEBUG] OWNER_KEY_HASH_ENV(initial)=${OWNER_KEY_HASH_ENV}${NC}" >&2
	echo -e "${YELLOW}[DEBUG] QUIET=${QUIET}${NC}" >&2
fi

# Show help
show_help() {
	if [ -n "$JSON_MODE" ]; then
		json_emit '{result:"help", commands:$cmds, globalOptions:$opts, example:$ex}' \
		  --argjson cmds '["create","remove","start","stop","restart","status","list","build","help"]' \
		  --argjson opts '["--json","--api-key","--api-key-hash","--non-interactive","--quiet","--debug"]' \
		  --arg ex "$0 create --api-key <KEY> --json"
	else
		echo -e "${GREEN}ALBERT | AI Sandbox Manager${NC}"
		echo -e "${GREEN}=======================${NC}"
		echo "Usage: $0 [COMMAND] [OPTIONS]"
		echo ""
		echo "Commands:"
		echo "  create [name]     - Creates a new sandbox container"
		echo "                      (without name, cryptic name will be generated)"
		echo "  remove <name>     - Removes a container"
		echo "  start <name>      - Starts a container"
		echo "  stop <name>       - Stops a container"
		echo "  restart <name>    - Restarts a container"
		echo "  status [name]     - Shows status of (a) container(s)"
		echo "  list              - Lists all containers"
		echo "  build             - Rebuilds the Docker image"
		echo "  help              - Shows this help"
		echo ""
		echo "Global Options:"
		echo "  --json                 JSON output (machine readable)"
		echo "  --api-key <PLAINTEXT>  Associate containers with API key (hashed)"
		echo "  --api-key-hash <HASH>  Provide pre-hashed key (sha256)"
		echo "  --non-interactive      Disable interactive prompts"
		echo "  --quiet                Suppress normal output"
		echo "  --debug                Debug diagnostics"
		echo ""
		echo "VNC Password: albert"
		echo ""
		echo "Examples:"
		echo "  $0 create                  # Creates container with cryptic name"
		echo "  $0 create mysandbox        # Creates container with custom name"
		echo "  $0 status"
		echo "  $0 list"
	fi
}

# Build Docker image
build_image() {
        local build_dir="/opt/albert-ai-sandbox-manager/docker"

        if [ -z "$JSON_MODE" ] && [ -z "$QUIET" ]; then
                echo -e "${YELLOW}Building Docker image...${NC}"
        fi

        if ! pushd "$build_dir" >/dev/null 2>&1; then
                local msg="Docker build context not found at $build_dir"
                if [ -n "$JSON_MODE" ]; then
                        json_error 2 "build_context_missing" "$msg"
                else
                        echo -e "${RED}$msg${NC}" >&2
                fi
                return 2
        fi

        docker build -t "$DOCKER_IMAGE" .
        local rc=$?
        popd >/dev/null 2>&1 || true

        if [ $rc -ne 0 ]; then
                if [ -n "$JSON_MODE" ]; then
                        json_error "$rc" "build_failed" "Docker build failed with exit code $rc"
                else
                        echo -e "${RED}Docker build failed (exit $rc)${NC}" >&2
                fi
                return $rc
        fi

        if [ -n "$JSON_MODE" ]; then
                json_emit '{result:"built", image:$img}' --arg img "$DOCKER_IMAGE"
        elif [ -z "$QUIET" ]; then
                echo -e "${GREEN}Image built successfully${NC}"
        fi
        return 0
}

# --- Schema + API Key validation ----------------------------------------------------
API_KEY_DB_ID=""

ensure_schema() {
	# Use python for reliable schema creation if available
	if command -v python3 >/dev/null 2>&1; then
		python3 - "$DB_PATH" <<'PY'
import os, sqlite3, sys
db_path = sys.argv[1] if len(sys.argv)>1 else os.environ.get('DB_PATH')
if not db_path:
    sys.exit(0)
os.makedirs(os.path.dirname(db_path), exist_ok=True)
conn = sqlite3.connect(db_path)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_hash TEXT UNIQUE NOT NULL,
  label TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS containers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  api_key_id INTEGER NOT NULL,
  container_id TEXT UNIQUE NOT NULL,
  name TEXT,
  image TEXT,
  created_at INTEGER NOT NULL,
  FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);
""")
conn.commit(); conn.close()
PY
	else
		# Fallback: try creating via sqlite3 CLI
		sqlite3 "$DB_PATH" "CREATE TABLE IF NOT EXISTS api_keys(id INTEGER PRIMARY KEY AUTOINCREMENT,key_hash TEXT UNIQUE NOT NULL,label TEXT,created_at INTEGER NOT NULL);" 2>/dev/null || true
	fi
}

lookup_api_key_id_python() {
	local hash="$1"
	python3 - <<PY 2>/dev/null || true
import sqlite3, os
db=os.environ.get('DB_PATH')
if not db or not os.path.exists(db):
		print("")
		raise SystemExit
con=sqlite3.connect(db)
cur=con.cursor()
cur.execute("SELECT id FROM api_keys WHERE key_hash=? LIMIT 1", ("$hash",))
r=cur.fetchone()
print(r[0] if r else "")
con.close()
PY
}

require_api_key() {
	if [ -z "$OWNER_KEY_HASH_ENV" ]; then
		json_error 2 "API key required" "This operation requires an API key. Use --api-key <PLAINTEXT> or --api-key-hash <HASH>."
	fi
	ensure_schema
	if [ ! -f "$DB_PATH" ]; then
		json_error 2 "DB missing" "Manager DB not found at $DB_PATH – cannot validate API key. Install or create key first."
	fi
	# Accept either plaintext (token) or already hashed 64-char hex
	local candidate="$OWNER_KEY_HASH_ENV"
	if [[ ! $candidate =~ ^[0-9a-fA-F]{64}$ ]]; then
		debug_log "Interpreting provided key as PLAINTEXT; hashing it"
		candidate=$(hash_plaintext_key "$candidate")
		debug_log "Derived hash=$candidate"
	fi
	if command -v python3 >/dev/null 2>&1; then
		API_KEY_DB_ID=$(DB_PATH="$DB_PATH" lookup_api_key_id_python "$candidate")
	else
		API_KEY_DB_ID=$(sqlite3 "$DB_PATH" "SELECT id FROM api_keys WHERE key_hash='$candidate' LIMIT 1;" 2>/dev/null || true)
	fi
	if [ -z "$API_KEY_DB_ID" ]; then
		if [ -n "$DEBUG" ]; then
			echo -e "${YELLOW}[DEBUG] Key not found. Existing key_hash prefixes:${NC}" >&2
			if command -v python3 >/dev/null 2>&1; then
				python3 - <<PY 2>/dev/null || true
import sqlite3, os
db=os.environ.get('DB_PATH')
if db and os.path.exists(db):
		con=sqlite3.connect(db); cur=con.cursor()
		try:
				for (h,) in cur.execute("SELECT substr(key_hash,1,12) FROM api_keys"): print('[DEBUG]   '+h)
		except Exception as e: print('[DEBUG]   (error listing keys)', e)
		con.close()
PY
			else
				sqlite3 "$DB_PATH" "SELECT substr(key_hash,1,12) FROM api_keys;" 2>/dev/null | sed 's/^/[DEBUG]   /' >&2 || true
			fi
			echo -e "${YELLOW}[DEBUG] Searched for hash: $candidate${NC}" >&2
		fi
		json_error 2 "Unknown API key" "Provided API key not registered."
	fi
	OWNER_KEY_HASH_ENV="$candidate"
	debug_log "Resolved API_KEY_DB_ID=$API_KEY_DB_ID using hash=$OWNER_KEY_HASH_ENV"
}

verify_container_ownership() {
        local name=$1
        if [ -z "$OWNER_KEY_HASH_ENV" ]; then
                json_error 2 "API key required" "Use --api-key/--api-key-hash for container-specific operations."
        fi
        local lbl
        if ! lbl=$(docker inspect -f '{{ index .Config.Labels "albert.apikey_hash" }}' "$name" 2>/dev/null); then
                json_error 3 "not_found" "Container '$name' not found."
        fi
        if [ "$lbl" = "<no value>" ] || [ -z "$lbl" ]; then
                json_error 3 "unmanaged_container" "Container '$name' is not managed by this sandbox (ownership label missing)."
        fi
        if [ "$lbl" != "$OWNER_KEY_HASH_ENV" ]; then
                json_error 3 "Ownership mismatch" "Container '$name' not owned by supplied API key."
        fi
}

# Unified JSON / text error helper
json_error() {
    local code="$1"; shift
    local short="$1"; shift
    local msg="$1"; shift || true
	if [ -n "$JSON_MODE" ]; then
		if command -v jq >/dev/null 2>&1; then
			jq -n --arg error "$short" --arg message "$msg" --arg code "$code" '{error:$error,message:$message,exitCode:($code|tonumber)}' && __ALBERT_JSON_EMITTED=1
		else
			# Minimal manual JSON fallback when jq is unavailable
			# Escape double quotes in strings (basic)
			local esc_short=${short//\\/\\\\}; esc_short=${esc_short//\"/\\\"}
			local esc_msg=${msg//\\/\\\\}; esc_msg=${esc_msg//\"/\\\"}
			printf '{"error":"%s","message":"%s","exitCode":%s}\n' "$esc_short" "$esc_msg" "$code"
			__ALBERT_JSON_EMITTED=1
		fi
	else
		echo -e "${RED}Error: $msg${NC}" >&2
	fi
    exit "$code"
}

wait_for_mcphub_ready() {
	local container_name="$1"
	local host_port="$2"
	local timeout="${ALBERT_MCPHUB_TIMEOUT:-120}"
	local interval="${ALBERT_MCPHUB_POLL_INTERVAL:-2}"
	local start_ts current elapsed probe_cmd

	MCPHUB_WAIT_ERROR=""

	case "$timeout" in
		''|*[!0-9]*) timeout=120 ;;
		*) : ;;
	esac
	case "$interval" in
		''|*[!0-9]*) interval=2 ;;
		*) : ;;
	esac
	if [ "$interval" -lt 1 ]; then
		interval=1
	fi

	if [ -z "$container_name" ]; then
		MCPHUB_WAIT_ERROR="Container name missing for MCP Hub wait"
		return 1
	fi

	if [ -z "$host_port" ]; then
		local port_line
		port_line=$(docker port "$container_name" 3000/tcp 2>/dev/null | head -n1)
		if [ -n "$port_line" ]; then
			host_port="${port_line##*:}"
			host_port="${host_port%%[^0-9]*}"
		fi
	fi

	if [ -z "$host_port" ]; then
		trace_log "wait_for_mcphub_ready: no port info for container='$container_name'"
		return 0
	fi

	if command -v curl >/dev/null 2>&1; then
		probe_cmd="curl"
	elif command -v wget >/dev/null 2>&1; then
		probe_cmd="wget"
	elif command -v nc >/dev/null 2>&1; then
		probe_cmd="nc"
	else
		trace_log "wait_for_mcphub_ready: no probe tool available; skipping wait"
		return 0
	fi

	if [ -z "$JSON_MODE" ] && [ -z "$QUIET" ]; then
		echo -e "${YELLOW}Waiting for MCP Hub in '${container_name}' (port ${host_port})...${NC}"
	fi

	start_ts=$(date +%s)
	while true; do
		case "$probe_cmd" in
			curl)
				if curl --silent --max-time 2 "http://127.0.0.1:${host_port}/" >/dev/null 2>&1; then
					break
				fi
				;;
			wget)
				if wget --quiet --spider --tries=1 --timeout=2 "http://127.0.0.1:${host_port}/" >/dev/null 2>&1; then
					break
				fi
				;;
			nc)
				if nc -z -w 2 127.0.0.1 "$host_port" >/dev/null 2>&1; then
					break
				fi
				;;
		esac

		current=$(date +%s)
		elapsed=$((current - start_ts))
		if [ "$elapsed" -ge "$timeout" ]; then
			MCPHUB_WAIT_ERROR="MCP Hub im Container '$container_name' wurde nach ${timeout}s nicht bereit."
			trace_log "wait_for_mcphub_ready: timeout container='$container_name' port='$host_port' timeout='${timeout}'"
			return 1
		fi
		sleep "$interval"
	done

	current=$(date +%s)
	elapsed=$((current - start_ts))
	trace_log "wait_for_mcphub_ready: ready container='$container_name' port='$host_port' method='$probe_cmd' elapsed='${elapsed}s'"
	if [ -z "$JSON_MODE" ] && [ -z "$QUIET" ]; then
		echo -e "${GREEN}MCP Hub bereit nach ${elapsed}s.${NC}"
	fi
	return 0
}

hash_plaintext_key() {
	# Hash a plaintext key (URL-safe base64 like token_urlsafe) deterministically
	if command -v python3 >/dev/null 2>&1; then
		python3 -c 'import sys,hashlib;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$1"
	else
		printf "%s" "$1" | openssl dgst -sha256 | awk '{print $2}'
	fi
}

# Generate a cryptic unique sandbox name if user omitted one
generate_random_name() {
	# Hardened: sbx- + 16 hex chars (8 bytes from /dev/urandom) to resist guessing
	local attempt suffix name
	for attempt in {1..20}; do
		suffix=$(head -c 8 /dev/urandom 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n' | cut -c1-16)
		[ -z "$suffix" ] && continue
		if [ ${#suffix} -lt 16 ]; then
			continue
		fi
		name="sbx-$suffix"
		if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${name}$" 2>/dev/null; then
			continue
		fi
		echo "$name"; return 0
	done
	# Fallback: derive deterministic 16-char suffix from high-resolution timestamp data
	local seed suffix_fallback
	seed="$(date +%s%N)-$$-${RANDOM:-0}"
	if command -v sha256sum >/dev/null 2>&1; then
		suffix_fallback=$(printf '%s' "$seed" | sha256sum | awk '{print $1}' | cut -c1-16)
	elif command -v openssl >/dev/null 2>&1; then
		suffix_fallback=$(printf '%s' "$seed" | openssl dgst -sha256 2>/dev/null | awk '{print $2}' | cut -c1-16)
	else
		suffix_fallback=$(printf '%s' "$seed" | tr -cd 'a-f0-9' | cut -c1-16)
	fi
	if [ -z "$suffix_fallback" ]; then
		suffix_fallback=$(printf '%s' "$seed" | tr -cd '[:alnum:]' | head -c 16)
	fi
	if [ ${#suffix_fallback} -lt 16 ]; then
		suffix_fallback=$(printf '%s' "$suffix_fallback$suffix_fallback" | head -c 16)
	fi
	echo "sbx-${suffix_fallback:-fallback}"
}

# Create container
create_container() {
	local name=$1
	trace_log "enter create_container name='$name' JSON_MODE='${JSON_MODE}'"

	if [ -z "$name" ]; then
		name=$(generate_random_name)
		debug_log "Generated random container name: $name"
		trace_log "generated name='$name'"
	fi

	# Ensure API key valid (uses global require_api_key)
	trace_log "before require_api_key key_hash_env='${OWNER_KEY_HASH_ENV}'"
	require_api_key
	trace_log "after require_api_key API_KEY_DB_ID='${API_KEY_DB_ID}' owner='${OWNER_KEY_HASH_ENV}'"
	# Check if container already exists (avoid pipe causing non-zero propagation issues)
        if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${name}$" 2>/dev/null; then
                trace_log "container already exists before creation name='$name'"
                json_error 1 "Exists" "Container '$name' already exists"
        fi
	debug_log "Resolved API_KEY_DB_ID=$API_KEY_DB_ID"
	
	# Find free ports
	local novnc_port=$(find_free_novnc_port)
	local vnc_port=$(find_free_vnc_port)
	local mcphub_port=$(find_free_mcphub_port)
	local filesvc_port=$(find_free_filesvc_port)

	# Ensure image exists before attempting run
	if ! docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
		trace_log "image missing '$DOCKER_IMAGE'"
		json_error 1 "Image missing" "Docker image '$DOCKER_IMAGE' not found. Run 'build' first."
	fi
	
	if [ -z "$novnc_port" ] || [ -z "$vnc_port" ] || [ -z "$mcphub_port" ] || [ -z "$filesvc_port" ]; then
		trace_log "port allocation failed novnc='$novnc_port' vnc='$vnc_port' mcphub='$mcphub_port' filesvc='$filesvc_port'"
		json_error 1 "No ports" "No free ports available"
	fi
	
        if [ -z "$JSON_MODE" ] && [ -z "$QUIET" ]; then
                echo -e "${YELLOW}Creating sandbox container '$name'...${NC}"
                echo -e "${BLUE}  noVNC Port: $novnc_port${NC}"
                echo -e "${BLUE}  VNC Port: $vnc_port${NC}"
                echo -e "${BLUE}  MCP Hub Port: $mcphub_port${NC}"
                echo -e "${BLUE}  File Service Port: $filesvc_port${NC}"
        fi
	
	LABEL_ARGS=(--label "albert.manager=1")
	if [ -n "$OWNER_KEY_HASH_ENV" ]; then
		LABEL_ARGS+=(--label "albert.apikey_hash=$OWNER_KEY_HASH_ENV")
	fi

	# Create Docker container
        docker run -d \
                "${LABEL_ARGS[@]}" \
                --name "$name" \
                --restart unless-stopped \
                --cap-add=SYS_ADMIN \
                --security-opt seccomp=unconfined \
		-p ${novnc_port}:6081 \
		-p ${vnc_port}:5901 \
		-p ${mcphub_port}:3000 \
		-p ${filesvc_port}:4000 \
		-e VNC_PORT=5901 \
		-e NO_VNC_PORT=6081 \
		-e MCP_HUB_PORT=3000 \
		-e FILE_SERVICE_PORT=4000 \
		-v ${name}_data:/home/ubuntu \
                --shm-size=2g \
                "$DOCKER_IMAGE" >/dev/null

        local run_rc=$?

	if [ $run_rc -eq 0 ]; then
		trace_log "docker run success name='$name'"
		if ! wait_for_mcphub_ready "$name" "$mcphub_port"; then
			if [ -n "$JSON_MODE" ]; then
				json_error 1 "mcphub_timeout" "${MCPHUB_WAIT_ERROR:-MCP Hub in container '$name' wurde nicht rechtzeitig bereit.}"
			else
				echo -e "${RED}${MCPHUB_WAIT_ERROR:-MCP Hub im Container '$name' wurde nicht rechtzeitig bereit.}${NC}" >&2
			fi
			return 1
		fi

		# Register in registry
		add_to_registry "$name" "$novnc_port" "$vnc_port" "$mcphub_port" "$filesvc_port"

		# Insert mapping into containers table (ignore if already exists)
		CONTAINER_ID=$(docker inspect -f '{{ .Id }}' "$name" 2>/dev/null || true)
		if [ -n "$CONTAINER_ID" ] && [ -n "$API_KEY_DB_ID" ]; then
			sqlite3 "$DB_PATH" "INSERT OR IGNORE INTO containers(api_key_id, container_id, name, image, created_at) VALUES($API_KEY_DB_ID,'$CONTAINER_ID','$name','$DOCKER_IMAGE', strftime('%s','now'));" 2>/dev/null || true
		fi
		
		# Configure nginx (includes file service)
		create_nginx_config "$name" "$novnc_port" "$mcphub_port" "$filesvc_port"
		
		# Create global MCP Hub configuration (only once)
		if [ ! -f "${NGINX_CONF_DIR}/albert-mcphub-global.conf" ]; then
			create_global_mcphub_config "$mcphub_port"
		fi
		
		if [ -n "$JSON_MODE" ]; then
			HOSTIP=$(hostname -I | awk '{print $1}')
			json_emit '{result:"created", name:$name, ownerHash:$ownerHash, ports:{novnc:$novnc_port,vnc:$vnc_port,mcphub:$mcphub_port,filesvc:$filesvc_port}, urls:{desktop:("http://"+$host+"/"+$name+"/"), mcphub:("http://"+$host+"/"+$name+"/mcphub/mcp"), filesUpload:("http://"+$host+"/"+$name+"/files/upload"), filesDownloadPattern:("http://"+$host+"/"+$name+"/files/download?path=/tmp/albert-files/<uuid.ext>")}}' \
				--arg name "$name" \
				--arg novnc_port "$novnc_port" \
				--arg vnc_port "$vnc_port" \
				--arg mcphub_port "$mcphub_port" \
				--arg filesvc_port "$filesvc_port" \
				--arg ownerHash "$OWNER_KEY_HASH_ENV" \
				--arg host "$HOSTIP"
		else
			echo -e "${GREEN}========================================${NC}"
			echo -e "${GREEN}Sandbox container created successfully!${NC}"
			echo -e "${GREEN}========================================${NC}"
			echo -e "${GREEN}Name: ${name}${NC}"
			echo -e "${GREEN}DESKTOP: http://$(hostname -I | awk '{print $1}')/${name}/${NC}"
			echo -e "${GREEN}MCP URL: http://$(hostname -I | awk '{print $1}')/${name}/mcphub/mcp${NC}"
			echo -e "${GREEN}File Service Upload: http://$(hostname -I | awk '{print $1}')/${name}/files/upload${NC}"
			echo -e "${GREEN}File Service Download: http://$(hostname -I | awk '{print $1}')/${name}/files/download?path=/tmp/albert-files/<uuid.ext>${NC}"
			echo -e "${YELLOW}MCP Hub Bearer token: albert${NC}"
			echo -e "${YELLOW}Important: Note the URL - the name is the access protection!${NC}"
		fi
        else
                trace_log "docker run failed name='$name' rc=$run_rc"
                json_error 1 "Create failed" "Error creating container"
        fi

	# Fallback: if JSON mode requested but nothing emitted (unexpected), emit generic error
        if [ -n "$JSON_MODE" ] && [ -z "$__ALBERT_JSON_EMITTED" ]; then
                trace_log "fallback JSON emitted name='$name'"
                printf '{"error":"internal","message":"No JSON emitted (fallback)","exitCode":1}\n'
                __ALBERT_JSON_EMITTED=1
                exit 1
        fi
}

# Remove container
remove_container() {
	local name=$1

	verify_container_ownership "$name"
	
	if [ -z "$name" ]; then json_error 1 "Missing name" "Container name required"; fi
	
        if [ -z "$JSON_MODE" ] && [ -z "$QUIET" ]; then
                echo -e "${YELLOW}Removing container '$name'...${NC}"
        fi

        # Stop and remove container
        docker stop "$name" >/dev/null
        docker rm "$name" >/dev/null

        if [ -z "$NON_INTERACTIVE" ]; then
                read -p "Also delete data volume? (y/n): " -n 1 -r; echo
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                        docker volume rm "${name}_data" >/dev/null
                fi
        fi
	
	# Remove nginx config
	remove_nginx_config "$name"
	
	# Remove from registry
	remove_from_registry "$name"
	
        if [ -n "$JSON_MODE" ]; then
                json_emit '{result:"removed", name:$n}' --arg n "$name"
        elif [ -z "$QUIET" ]; then
                echo -e "${GREEN}Container '$name' has been removed${NC}"
        fi
}

# Start container
start_container() {
	local name=$1

	verify_container_ownership "$name"
	
	if [ -z "$name" ]; then json_error 1 "Missing name" "Container name required"; fi
	
        if [ -z "$JSON_MODE" ] && [ -z "$QUIET" ]; then
                echo -e "${YELLOW}Starting container '$name'...${NC}"
        fi

        docker start "$name" >/dev/null
        local rc=$?

	if [ $rc -eq 0 ]; then
		local info=$(get_container_info "$name")
		local mcphub_port=""
		if [ -n "$info" ]; then
			if command -v jq >/dev/null 2>&1; then
				mcphub_port=$(printf '%s\n' "$info" | jq -r '.mcphub_port // empty')
			else
				mcphub_port=$(printf '%s\n' "$info" | sed -n 's/.*"mcphub_port"[[:space:]]*:[[:space:]]*"\([0-9]*\)".*/\1/p' | head -n1)
			fi
		fi
		if ! wait_for_mcphub_ready "$name" "$mcphub_port"; then
			if [ -n "$JSON_MODE" ]; then
				json_error 1 "mcphub_timeout" "${MCPHUB_WAIT_ERROR:-MCP Hub in container '$name' wurde nicht rechtzeitig bereit.}"
			else
				echo -e "${RED}${MCPHUB_WAIT_ERROR:-MCP Hub im Container '$name' wurde nicht rechtzeitig bereit.}${NC}" >&2
			fi
			return 1
		fi
		if [ -n "$JSON_MODE" ]; then
			local hostip=$(hostname -I | awk '{print $1}')
			json_emit '{result:"started", name:$n, url:($h+"/"+$n+"/"), host:$h}' --arg n "$name" --arg h "http://$hostip"
		elif [ -z "$QUIET" ]; then
			echo -e "${GREEN}Container '$name' started${NC}"
			echo -e "${GREEN}URL: http://$(hostname -I | awk '{print $1}')/${name}/${NC}"
		fi
        else
                if [ -n "$JSON_MODE" ]; then
                        json_error "$rc" "start_failed" "Docker failed to start container '$name' (exit $rc)"
                else
                        echo -e "${RED}Error starting container${NC}" >&2
                fi
                return $rc
        fi
}

# Stop container
stop_container() {
	local name=$1

	verify_container_ownership "$name"
	
	if [ -z "$name" ]; then json_error 1 "Missing name" "Container name required"; fi
	
        if [ -z "$JSON_MODE" ] && [ -z "$QUIET" ]; then
                echo -e "${YELLOW}Stopping container '$name'...${NC}"
        fi

        docker stop "$name" >/dev/null
        local rc=$?

        if [ $rc -eq 0 ]; then
                if [ -n "$JSON_MODE" ]; then
                        json_emit '{result:"stopped", name:$n}' --arg n "$name"
                elif [ -z "$QUIET" ]; then
                        echo -e "${GREEN}Container '$name' stopped${NC}"
                fi
        else
                if [ -n "$JSON_MODE" ]; then
                        json_error "$rc" "stop_failed" "Docker failed to stop container '$name' (exit $rc)"
                else
                        echo -e "${RED}Error stopping container${NC}" >&2
                fi
                return $rc
        fi
}

# Restart container
restart_container() {
	local name=$1

	verify_container_ownership "$name"
	
	if [ -z "$name" ]; then json_error 1 "Missing name" "Container name required"; fi
	
	stop_container "$name" >/dev/null 2>&1 || true
	start_container "$name" >/dev/null 2>&1 || true
        if [ -n "$JSON_MODE" ]; then
                json_emit '{result:"restarted", name:$n}' --arg n "$name"
        elif [ -z "$QUIET" ]; then
                echo -e "${GREEN}Container '$name' restarted${NC}"
        fi
}

# Show status
show_status() {
	local name=$1

	# Enforce API key also for status (list or single) to avoid leaking names
	require_api_key

                if [ -z "$name" ]; then
                # All containers
                if [ -n "$JSON_MODE" ]; then
                        local rows=()
                        for container_name in $(get_all_containers); do
				if [ -n "$OWNER_KEY_HASH_ENV" ]; then
					local lbl=$(docker inspect -f '{{ index .Config.Labels "albert.apikey_hash"}}' "$container_name" 2>/dev/null || true)
					[ "$lbl" != "$OWNER_KEY_HASH_ENV" ] && continue
				fi
				local sj=$(show_single_status "$container_name" json)
				[ -n "$sj" ] && rows+=("$sj")
			done
                        if [ ${#rows[@]} -eq 0 ]; then
                                printf '[]\n'
                        else
                                printf '['
                                for i in "${!rows[@]}"; do
                                        printf '%s' "${rows[$i]}"
                                        if [ $i -lt $(( ${#rows[@]} - 1 )) ]; then printf ','; fi
                                done
                                printf ']\n'
                        fi
                        __ALBERT_JSON_EMITTED=1
		else
			echo -e "${GREEN}Status of all sandbox containers:${NC}"
			echo -e "${GREEN}=================================${NC}"
			echo -e "${BLUE}Desktop: KDE Plasma${NC}"
			echo "------------------------------"
			for container_name in $(get_all_containers); do
				show_single_status "$container_name"
				echo "------------------------------"
			done
		fi
	else
		if [ -n "$JSON_MODE" ]; then
			show_single_status "$name" json
		else
			show_single_status "$name"
		fi
	fi
}

# Show single container status
show_single_status() {
	local name=$1
	local mode=${2:-text}
	local info=$(get_container_info "$name")
	if [ -z "$info" ]; then
		if [ "$mode" = "json" ] || [ -n "$JSON_MODE" ]; then
			json_emit '{error:"not_found", message:$m, name:$n}' --arg m "Container not found" --arg n "$name"
			return 0
		fi
		echo -e "${RED}Container '$name' not found in registry${NC}"
		return 1
	fi
	local port=$(echo "$info" | jq -r '.port')
	local vnc_port=$(echo "$info" | jq -r '.vnc_port')
	local mcphub_port=$(echo "$info" | jq -r '.mcphub_port // empty')
	local filesvc_port=$(echo "$info" | jq -r '.filesvc_port // empty')
	local created=$(echo "$info" | jq -r '.created')
	local running="stopped"
	local stats=""
	if docker ps --format '{{.Names}}' | grep -q "^${name}$"; then
		running="running"
		if [ -z "$STATUS_SKIP_STATS" ]; then
			stats=$(docker stats --no-stream --format "CPU: {{.CPUPerc}} | RAM: {{.MemUsage}}" "$name" 2>/dev/null || true)
		else
			stats=""
		fi
	fi
	local hostip=$(hostname -I | awk '{print $1}')
        if [ "$mode" = "json" ] || [ -n "$JSON_MODE" ]; then
                jq -n \
                        --arg name "$name" \
                        --arg status "$running" \
                        --arg created "$created" \
                        --arg novnc "$port" \
                        --arg vnc "$vnc_port" \
			--arg mcphub "$mcphub_port" \
			--arg filesvc "$filesvc_port" \
			--arg stats "$stats" \
			--arg ownerHash "$OWNER_KEY_HASH_ENV" \
			--arg host "$hostip" \
                        '{name:$name,status:$status,created:$created,ownerHash:$ownerHash,ports:{novnc:$novnc,vnc:$vnc,mcphub:$mcphub,filesvc:$filesvc},resources:$stats,urls:{desktop:("http://"+$host+"/"+$name+"/"), mcphub:("http://"+$host+"/"+$name+"/mcphub/mcp"), files:("http://"+$host+"/"+$name+"/files/")}}'
                __ALBERT_JSON_EMITTED=1
        else
                echo -e "${BLUE}Container: ${NC}$name"
		echo -e "${BLUE}Created: ${NC}$created"
		echo -e "${BLUE}Desktop: ${NC}KDE Plasma"
		echo -e "${BLUE}noVNC Port: ${NC}$port"
		echo -e "${BLUE}VNC Port: ${NC}$vnc_port"
		if [ "$running" = "running" ]; then
			echo -e "${BLUE}Docker Status: ${GREEN}Running${NC}"
			[ -n "$stats" ] && echo -e "${BLUE}Resources: ${NC}$stats"
		else
			echo -e "${BLUE}Docker Status: ${RED}Stopped${NC}"
		fi
		echo -e "${BLUE}URL: ${NC}http://${hostip}/${name}/"
	fi
}

# List containers
list_containers() {
        require_api_key
        debug_log "Listing containers for key_hash=$OWNER_KEY_HASH_ENV"
        local print_header=1
        [ -n "$JSON_MODE" ] && print_header=0
        [ -n "$QUIET" ] && print_header=0
        if [ $print_header -eq 1 ]; then
                echo -e "${GREEN}ALBERT Sandbox Containers:${NC}"
                echo -e "${GREEN}========================================${NC}"
                printf "%-30s %-10s %-10s %-10s\n" "NAME" "STATUS" "NOVNC-PORT" "VNC-PORT"
                printf "%-30s %-10s %-10s %-10s\n" "----" "------" "----------" "--------"
        fi
	
	FILTERED=( $(get_all_containers) )
	if [ -n "$OWNER_KEY_HASH_ENV" ]; then
		TMP=()
		for nm in "${FILTERED[@]}"; do
			LBL=$(docker inspect -f '{{ index .Config.Labels "albert.apikey_hash"}}' "$nm" 2>/dev/null || true)
			if [ "$LBL" = "$OWNER_KEY_HASH_ENV" ]; then TMP+=("$nm"); fi
		done
		FILTERED=("${TMP[@]}")
	fi
	JSON_ROWS=()
	for container_name in "${FILTERED[@]}"; do
		local info=$(get_container_info "$container_name")
                local port=$(echo "$info" | jq -r '.port')
                local vnc_port=$(echo "$info" | jq -r '.vnc_port')

                if docker ps --format '{{.Names}}' | grep -q "^${container_name}$"; then
                                local status="${GREEN}Running${NC}"
                else
                                local status="${RED}Stopped${NC}"
                fi

                if [ -n "$JSON_MODE" ]; then
                        mcphub_port=$(echo "$info" | jq -r '.mcphub_port // empty')
                        filesvc_port=$(echo "$info" | jq -r '.filesvc_port // empty')
                        plain_status=$(docker ps --format '{{.Names}}' | grep -q "^${container_name}$" && echo running || echo stopped)
                        JSON_ROWS+=( "$(jq -n --arg name "$container_name" --arg status "$plain_status" --arg novnc "$port" --arg vnc "$vnc_port" --arg mcphub "$mcphub_port" --arg filesvc "$filesvc_port" --arg ownerHash "$OWNER_KEY_HASH_ENV" '{name:$name,status:$status,ownerHash:$ownerHash,ports:{novnc:$novnc,vnc:$vnc,mcphub:$mcphub,filesvc:$filesvc}}')" )
                elif [ -n "$QUIET" ]; then
                        printf '%s\n' "$container_name"
                else
                        printf "%-30s %-20b %-10s %-10s\n" "$container_name" "$status" "$port" "$vnc_port"
                fi
        done
        if [ -n "$JSON_MODE" ]; then
                if [ ${#JSON_ROWS[@]} -eq 0 ]; then
                        printf '[]\n'
                else
                        printf '['
                        for i in "${!JSON_ROWS[@]}"; do
                                printf '%s' "${JSON_ROWS[$i]}"
                                if [ $i -lt $(( ${#JSON_ROWS[@]} - 1 )) ]; then printf ','; fi
                        done
                        printf ']\n'
                fi
                __ALBERT_JSON_EMITTED=1
        elif [ -z "$QUIET" ]; then
                if [ ${#FILTERED[@]} -eq 0 ]; then
                        echo -e "${YELLOW}(No containers for this API key)${NC}"
                else
                        echo ""
                        echo -e "${BLUE}Desktop: KDE Plasma | VNC Password: albert${NC}"
			echo ""
			echo -e "${BLUE}Access URLs:${NC}"
			for container_name in "${FILTERED[@]}"; do
				echo "  http://$(hostname -I | awk '{print $1}')/${container_name}/"
			done
		fi
	fi
}

# Main program
# Use a safe default for $1 to avoid "unbound variable" errors when no argument is provided
case "${1:-}" in
	create)
		create_container "${2:-}"
		;;
	selfcheck)
		# Lightweight diagnostics: DB, schema, keys
		[ -z "$JSON_MODE" ] && echo "=== selfcheck ==="
		[ -z "$JSON_MODE" ] && echo "DB_PATH: $DB_PATH"
		# Ensure schema before inspection
		ensure_schema
		if [ -f "$DB_PATH" ]; then
			if stat -c%s "$DB_PATH" >/dev/null 2>&1; then sz=$(stat -c%s "$DB_PATH"); else sz=$(stat -f%z "$DB_PATH" 2>/dev/null || echo ?); fi
			[ -z "$JSON_MODE" ] && echo "DB exists: yes (size ${sz} bytes)"
		else
			if [ -n "$JSON_MODE" ]; then json_emit '{error:"db_missing",message:"DB not found",result:"error"}'; else echo "DB exists: no"; fi; exit 2
		fi
                # Validate header signature
                if head -c 16 "$DB_PATH" 2>/dev/null | LC_ALL=C grep -aq "SQLite format 3"; then
                        [ -z "$JSON_MODE" ] && echo "Header: OK (SQLite format 3)"
                else
                        [ -z "$JSON_MODE" ] && echo "Header: WARNING (unexpected first 16 bytes)"
                fi
		# Extra diagnostics
		[ -z "$JSON_MODE" ] && stat "$DB_PATH" 2>/dev/null | sed 's/^/STAT: /'
		if [ -z "$JSON_MODE" ] && command -v realpath >/dev/null 2>&1; then echo "Realpath: $(realpath "$DB_PATH")"; fi
		if [ -z "$JSON_MODE" ]; then echo -n "First 64 bytes (hex): "; hexdump -Cv "$DB_PATH" 2>/dev/null | head -n1 || echo "(hexdump unavailable)"; fi
		# sqlite3 CLI diagnostics
		if command -v sqlite3 >/dev/null 2>&1; then
			if [ -z "$JSON_MODE" ]; then SQLITE_VER=$(sqlite3 -version 2>&1 || true); echo "sqlite3 version: $SQLITE_VER"; fi
			# Capture stderr separately for .tables
			SQLITE_TABLES_OUT=$(sqlite3 "$DB_PATH" ".tables" 2> /tmp/.albert_sqlite_tables_err.$$ || true)
				if [ -z "$JSON_MODE" ] && [ -s /tmp/.albert_sqlite_tables_err.$$ ]; then echo "Tables (sqlite3 .tables) stderr:"; sed 's/^/  ERR: /' /tmp/.albert_sqlite_tables_err.$$; fi
			rm -f /tmp/.albert_sqlite_tables_err.$$ 2>/dev/null || true
				if [ -z "$JSON_MODE" ]; then echo "Tables (sqlite3 .tables):"; if [ -n "$SQLITE_TABLES_OUT" ]; then printf '%s\n' "$SQLITE_TABLES_OUT" | sed 's/^/  /'; else echo "  (none)"; fi; fi
		else
			[ -z "$JSON_MODE" ] && echo "sqlite3 CLI not installed"
		fi
                # Python view of tables & counts
                if [ -z "$JSON_MODE" ] && command -v python3 >/dev/null 2>&1; then
                        python3 - "$DB_PATH" <<'PY' 2>/dev/null || true
import os, sqlite3, time
db = os.environ.get('DB_PATH') or (len(sys.argv)>1 and sys.argv[1])
print('Tables (python query):')
if not db or not os.path.exists(db):
    print('  (db missing)')
else:
    con=sqlite3.connect(db)
    cur=con.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        rows=cur.fetchall()
        if not rows:
            print('  (none)')
        else:
            for (n,) in rows: print('  '+n)
        # List API keys if table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='api_keys'")
        if cur.fetchone():
            print('API keys:')
            for r in cur.execute("SELECT id, substr(key_hash,1,12), label, datetime(created_at,'unixepoch') FROM api_keys ORDER BY created_at DESC"):
                # r[2] may be None
                label = r[2] or ''
                print(f"  id={r[0]} prefix={r[1]} label={label} created={r[3]}")
        else:
            print('API keys: (table missing)')
    except Exception as e:
        print('  (error reading)', e)
    finally:
        con.close()
PY
                elif [ -z "$JSON_MODE" ]; then
                        echo "(python3 not available for deep inspection)"
                fi
		# Legacy sqlite listing (kept for comparison)
		if command -v sqlite3 >/dev/null 2>&1; then
			SQLITE_KEYS_OUT=$(sqlite3 "$DB_PATH" "SELECT id, substr(key_hash,1,12), label, datetime(created_at,'unixepoch') FROM api_keys ORDER BY created_at DESC;" 2> /tmp/.albert_sqlite_keys_err.$$ || true)
			if [ -z "$JSON_MODE" ] && [ -s /tmp/.albert_sqlite_keys_err.$$ ]; then echo "API keys (.sqlite3 direct) stderr:"; sed 's/^/  ERR: /' /tmp/.albert_sqlite_keys_err.$$; fi
			rm -f /tmp/.albert_sqlite_keys_err.$$ 2>/dev/null || true
			[ -z "$JSON_MODE" ] && {
				echo "API keys (.sqlite3 direct):"
				if [ -n "$SQLITE_KEYS_OUT" ]; then printf '%s\n' "$SQLITE_KEYS_OUT" | awk 'BEGIN{FS="|"}{printf "  id=%s prefix=%s label=%s created=%s\n", $1,$2,$3,$4}'; else echo "  (query failed)"; fi
			}
		fi
                if [ -z "$JSON_MODE" ] && [ -n "$OWNER_KEY_HASH_ENV" ]; then
                        inp="$OWNER_KEY_HASH_ENV"
                        if [[ $inp =~ ^[0-9a-fA-F]{64}$ ]]; then
                                candidate="$inp"
                        else
                                candidate=$(hash_plaintext_key "$inp")
				echo "Hashed provided plaintext -> $candidate"
			fi
			m=$(sqlite3 "$DB_PATH" "SELECT id FROM api_keys WHERE key_hash='$candidate' LIMIT 1;" 2>/dev/null || true)
			if [ -n "$m" ]; then echo "Lookup: MATCH (id=$m)"; else echo "Lookup: NO MATCH for $candidate"; fi
		fi
                if [ -n "$JSON_MODE" ]; then
                        selfcheck_json=""
                        if command -v python3 >/dev/null 2>&1; then
                                selfcheck_json=$(
python3 - "$DB_PATH" <<'PY' 2>/dev/null
import os, sqlite3, json, sys, time
db = os.environ.get('DB_PATH') or (len(sys.argv)>1 and sys.argv[1])
out = {"result":"ok","dbPath":db,"tables":[],"apiKeys":[]}
if db and os.path.exists(db):
        try:
                con=sqlite3.connect(db); cur=con.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                out["tables"]=[r[0] for r in cur.fetchall()]
                if 'api_keys' in out["tables"]:
                        for r in cur.execute("SELECT id, substr(key_hash,1,12), label, created_at FROM api_keys ORDER BY created_at DESC"):
                                out["apiKeys"].append({"id":r[0],"prefix":r[1],"label":r[2] or '',"created_at":r[3]})
        except Exception as e:
                out["error"]=str(e); out["result"]="error"
        finally:
                try: con.close()
                except: pass
print(json.dumps(out))
PY
                                )
                                sc_rc=$?
                                if [ $sc_rc -ne 0 ]; then
                                        selfcheck_json=""
                                fi
                        fi
                        if [ -n "$selfcheck_json" ]; then
                                printf '%s\n' "$selfcheck_json"
                                __ALBERT_JSON_EMITTED=1
                        else
                                json_emit '{result:"ok",dbPath:$path,tables:[],apiKeys:[]}' --arg path "$DB_PATH"
                        fi
                fi
                exit 0
                ;;
	dbtrace)
		# Deeper DB diagnostics
		echo "=== dbtrace ==="
		echo "DB_PATH: $DB_PATH"
		if [ ! -f "$DB_PATH" ]; then echo "DB does not exist"; exit 1; fi
		stat "$DB_PATH" 2>/dev/null | sed 's/^/STAT: /'
		if command -v realpath >/dev/null 2>&1; then echo "Realpath: $(realpath "$DB_PATH")"; fi
		if command -v sqlite3 >/dev/null 2>&1; then
			for q in 'PRAGMA schema_version' 'PRAGMA user_version' 'PRAGMA page_size' 'PRAGMA freelist_count' "SELECT count(*) AS api_keys FROM api_keys" "SELECT count(*) AS containers FROM containers"; do
				printf 'Query: %s -> ' "$q"
				sqlite3 "$DB_PATH" "$q" 2>/dev/null || echo "(error)"
			done
			printf 'Integrity: '
			sqlite3 "$DB_PATH" 'PRAGMA integrity_check;' 2>/dev/null
		else
			echo "sqlite3 CLI not installed; limited dbtrace"
		fi
		exit 0
		;;
	remove|delete)
		remove_container "${2:-}"
		;;
	start)
		start_container "${2:-}"
		;;
	stop)
		stop_container "${2:-}"
		;;
	restart)
		restart_container "${2:-}"
		;;
	status)
		show_status "${2:-}"
		;;
	list)
		list_containers
		;;
	build)
		build_image
		;;
	help|--help|-h|"")
		show_help
		;;
	*)
		echo -e "${RED}Unknown command: ${1:-}(none)${NC}"
		show_help
		exit 1
		;;
esac
