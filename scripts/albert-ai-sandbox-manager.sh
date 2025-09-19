#!/bin/bash

source /opt/albert-ai-sandbox-manager/scripts/common.sh
source /opt/albert-ai-sandbox-manager/scripts/port-manager.sh
source /opt/albert-ai-sandbox-manager/scripts/nginx-manager.sh

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

# Parse optional global flags (support both before and after command)
ORIG_ARGS=("$@")
FIRST_PASS=()
COMMAND_SEEN=""
while [[ $# -gt 0 ]]; do
	case "$1" in
		--json) JSON_MODE=1; shift ;;
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

if [ ${#FIRST_PASS[@]} -eq 0 ]; then
	set -- "${ORIG_ARGS[@]}"
else
	set -- "${FIRST_PASS[@]}"
fi

# Second pass: if command present, allow flags after it
if [[ -n "$COMMAND_SEEN" ]]; then
	CMD="$1"; shift
	POST_FLAGS=()
	while [[ $# -gt 0 ]]; do
		case "$1" in
			--json) JSON_MODE=1; shift ;;
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
fi

# Show help
show_help() {
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
	echo ""
	echo "VNC Password: albert"
	echo ""
	echo "Examples:"
	echo "  $0 create                  # Creates container with cryptic name"
	echo "  $0 create mysandbox        # Creates container with custom name"
	echo "  $0 status"
	echo "  $0 list"
}

# Build Docker image
build_image() {
	echo -e "${YELLOW}Building Docker image...${NC}"
	cd /opt/albert-ai-sandbox-manager/docker
	docker build -t $DOCKER_IMAGE .
	echo -e "${GREEN}Image built successfully${NC}"
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
	local lbl=$(docker inspect -f '{{ index .Config.Labels "albert.apikey_hash" }}' "$name" 2>/dev/null || true)
	if [ -z "$lbl" ]; then
		json_error 3 "Ownership label missing" "Container '$name' has no ownership label – access denied."
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
        jq -n --arg error "$short" --arg message "$msg" --arg code "$code" '{error:$error,message:$message,exitCode:($code|tonumber)}'
    else
        echo -e "${RED}Error: $msg${NC}" >&2
    fi
    exit "$code"
}

hash_plaintext_key() {
	# Hash a plaintext key (URL-safe base64 like token_urlsafe) deterministically
	if command -v python3 >/dev/null 2>&1; then
		python3 -c 'import sys,hashlib;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$1"
	else
		printf "%s" "$1" | openssl dgst -sha256 | awk '{print $2}'
	fi
}

# Create container
create_container() {
	local name=$1

	# Ensure API key valid (uses global require_api_key)
	require_api_key
	# Check if container already exists
	if docker ps -a --format '{{.Names}}' | grep -q "^${name}$"; then
		json_error 1 "Exists" "Container '$name' already exists"
	fi
	debug_log "Resolved API_KEY_DB_ID=$API_KEY_DB_ID"
	
	# Find free ports
	local novnc_port=$(find_free_novnc_port)
	local vnc_port=$(find_free_vnc_port)
	local mcphub_port=$(find_free_mcphub_port)
	local filesvc_port=$(find_free_filesvc_port)
	
	if [ -z "$novnc_port" ] || [ -z "$vnc_port" ] || [ -z "$mcphub_port" ] || [ -z "$filesvc_port" ]; then
		json_error 1 "No ports" "No free ports available"
	fi
	
	echo -e "${YELLOW}Creating sandbox container '$name'...${NC}"
	echo -e "${BLUE}  noVNC Port: $novnc_port${NC}"
	echo -e "${BLUE}  VNC Port: $vnc_port${NC}"
	echo -e "${BLUE}  MCP Hub Port: $mcphub_port${NC}"
	echo -e "${BLUE}  File Service Port: $filesvc_port${NC}"
	
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
		$DOCKER_IMAGE
	
	if [ $? -eq 0 ]; then
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
			jq -n \
				--arg name "$name" \
				--arg novnc_port "$novnc_port" \
				--arg vnc_port "$vnc_port" \
				--arg mcphub_port "$mcphub_port" \
				--arg filesvc_port "$filesvc_port" \
				--arg ownerHash "$OWNER_KEY_HASH_ENV" \
				--arg host "$HOSTIP" \
				'{result:"created", name:$name, ownerHash:$ownerHash, ports:{novnc:$novnc_port,vnc:$vnc_port,mcphub:$mcphub_port,filesvc:$filesvc_port}, urls:{desktop:("http://"+$host+"/"+$name+"/"), mcphub:("http://"+$host+"/"+$name+"/mcphub/mcp"), filesUpload:("http://"+$host+"/"+$name+"/files/upload"), filesDownloadPattern:("http://"+$host+"/"+$name+"/files/download?path=/tmp/albert-files/<uuid.ext>")}}'
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
		json_error 1 "Create failed" "Error creating container"
	fi
}

# Remove container
remove_container() {
	local name=$1

	verify_container_ownership "$name"
	
	if [ -z "$name" ]; then json_error 1 "Missing name" "Container name required"; fi
	
	echo -e "${YELLOW}Removing container '$name'...${NC}"
	
	# Stop and remove container
	docker stop "$name" 2>/dev/null
	docker rm "$name" 2>/dev/null
	
	if [ -z "$NON_INTERACTIVE" ]; then
		read -p "Also delete data volume? (y/n): " -n 1 -r; echo
		if [[ $REPLY =~ ^[Yy]$ ]]; then
			docker volume rm "${name}_data" 2>/dev/null
		fi
	fi
	
	# Remove nginx config
	remove_nginx_config "$name"
	
	# Remove from registry
	remove_from_registry "$name"
	
	echo -e "${GREEN}Container '$name' has been removed${NC}"
}

# Start container
start_container() {
	local name=$1

	verify_container_ownership "$name"
	
	if [ -z "$name" ]; then json_error 1 "Missing name" "Container name required"; fi
	
	echo -e "${YELLOW}Starting container '$name'...${NC}"
	docker start "$name"
	
	if [ $? -eq 0 ]; then
		local info=$(get_container_info "$name")
		echo -e "${GREEN}Container '$name' started${NC}"
		echo -e "${GREEN}URL: http://$(hostname -I | awk '{print $1}')/${name}/${NC}"
	else
		echo -e "${RED}Error starting container${NC}"
		return 1
	fi
}

# Stop container
stop_container() {
	local name=$1

	verify_container_ownership "$name"
	
	if [ -z "$name" ]; then json_error 1 "Missing name" "Container name required"; fi
	
	echo -e "${YELLOW}Stopping container '$name'...${NC}"
	docker stop "$name"
	
	if [ $? -eq 0 ]; then
		echo -e "${GREEN}Container '$name' stopped${NC}"
	else
		echo -e "${RED}Error stopping container${NC}"
		return 1
	fi
}

# Restart container
restart_container() {
	local name=$1

	verify_container_ownership "$name"
	
	if [ -z "$name" ]; then json_error 1 "Missing name" "Container name required"; fi
	
	stop_container "$name"
	start_container "$name"
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
			printf '['
			for i in "${!rows[@]}"; do
				printf '%s' "${rows[$i]}"
				if [ $i -lt $(( ${#rows[@]} - 1 )) ]; then printf ','; fi
			done
			printf ']'
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
		show_single_status "$name"
	fi
}

# Show single container status
show_single_status() {
	local name=$1
	local mode=${2:-text}
	local info=$(get_container_info "$name")
	if [ -z "$info" ]; then
		[ "$mode" = "json" ] && return 0
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
		stats=$(docker stats --no-stream --format "CPU: {{.CPUPerc}} | RAM: {{.MemUsage}}" "$name" 2>/dev/null || true)
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
	echo -e "${GREEN}ALBERT Sandbox Containers:${NC}"
	echo -e "${GREEN}========================================${NC}"
	
	printf "%-30s %-10s %-10s %-10s\n" "NAME" "STATUS" "NOVNC-PORT" "VNC-PORT"
	printf "%-30s %-10s %-10s %-10s\n" "----" "------" "----------" "--------"
	
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
		else
			printf "%-30s %-20b %-10s %-10s\n" "$container_name" "$status" "$port" "$vnc_port"
		fi
	done
	if [ -n "$JSON_MODE" ]; then
		printf '['
		for i in "${!JSON_ROWS[@]}"; do
			printf '%s' "${JSON_ROWS[$i]}"
			if [ $i -lt $(( ${#JSON_ROWS[@]} - 1 )) ]; then printf ','; fi
		done
		printf ']'
	else
		echo ""
		echo -e "${BLUE}Desktop: KDE Plasma | VNC Password: albert${NC}"
		echo ""
		echo -e "${BLUE}Access URLs:${NC}"
		for container_name in "${FILTERED[@]}"; do
			echo "  http://$(hostname -I | awk '{print $1}')/${container_name}/"
		done
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
		echo "=== selfcheck ==="
		echo "DB_PATH: $DB_PATH"
		# Ensure schema before inspection
		ensure_schema
		if [ -f "$DB_PATH" ]; then
			if stat -c%s "$DB_PATH" >/dev/null 2>&1; then sz=$(stat -c%s "$DB_PATH"); else sz=$(stat -f%z "$DB_PATH" 2>/dev/null || echo ?); fi
			echo "DB exists: yes (size ${sz} bytes)"
		else
			echo "DB exists: no"; exit 2
		fi
		# Validate header signature
		head_sig=$(head -c 16 "$DB_PATH" 2>/dev/null || true)
		if echo "$head_sig" | grep -q "SQLite format 3"; then
			echo "Header: OK (SQLite format 3)"
		else
			echo "Header: WARNING (unexpected first 16 bytes)"
		fi
		# Extra diagnostics
		stat "$DB_PATH" 2>/dev/null | sed 's/^/STAT: /'
		if command -v realpath >/dev/null 2>&1; then echo "Realpath: $(realpath "$DB_PATH")"; fi
		echo -n "First 64 bytes (hex): "; hexdump -Cv "$DB_PATH" 2>/dev/null | head -n1 || echo "(hexdump unavailable)"
		# sqlite3 CLI diagnostics
		if command -v sqlite3 >/dev/null 2>&1; then
			SQLITE_VER=$(sqlite3 -version 2>&1 || true)
			echo "sqlite3 version: $SQLITE_VER"
			# Capture stderr separately for .tables
			SQLITE_TABLES_OUT=$(sqlite3 "$DB_PATH" ".tables" 2> /tmp/.albert_sqlite_tables_err.$$ || true)
			if [ -s /tmp/.albert_sqlite_tables_err.$$ ]; then
				echo "Tables (sqlite3 .tables) stderr:"; sed 's/^/  ERR: /' /tmp/.albert_sqlite_tables_err.$$
			fi
			rm -f /tmp/.albert_sqlite_tables_err.$$ 2>/dev/null || true
			echo "Tables (sqlite3 .tables):"
			if [ -n "$SQLITE_TABLES_OUT" ]; then printf '%s\n' "$SQLITE_TABLES_OUT" | sed 's/^/  /'; else echo "  (none)"; fi
		else
			echo "sqlite3 CLI not installed"
		fi
		# Python view of tables & counts
 		if command -v python3 >/dev/null 2>&1; then
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
		else
			echo "(python3 not available for deep inspection)"
		fi
		# Legacy sqlite listing (kept for comparison)
		if command -v sqlite3 >/dev/null 2>&1; then
			SQLITE_KEYS_OUT=$(sqlite3 "$DB_PATH" "SELECT id, substr(key_hash,1,12), label, datetime(created_at,'unixepoch') FROM api_keys ORDER BY created_at DESC;" 2> /tmp/.albert_sqlite_keys_err.$$ || true)
			if [ -s /tmp/.albert_sqlite_keys_err.$$ ]; then
				echo "API keys (.sqlite3 direct) stderr:"; sed 's/^/  ERR: /' /tmp/.albert_sqlite_keys_err.$$
			fi
			rm -f /tmp/.albert_sqlite_keys_err.$$ 2>/dev/null || true
			echo "API keys (.sqlite3 direct):"
			if [ -n "$SQLITE_KEYS_OUT" ]; then printf '%s\n' "$SQLITE_KEYS_OUT" | awk 'BEGIN{FS="|"}{printf "  id=%s prefix=%s label=%s created=%s\n", $1,$2,$3,$4}'; else echo "  (query failed)"; fi
		else
			echo "API keys (.sqlite3 direct): sqlite3 not installed"
		fi
		if [ -n "$OWNER_KEY_HASH_ENV" ]; then
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
