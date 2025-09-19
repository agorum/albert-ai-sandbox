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

# Extended modes
JSON_MODE="${ALBERT_JSON:-}"          # set to any non-empty for JSON output
OWNER_KEY_HASH_ENV="${ALBERT_OWNER_KEY_HASH:-}"  # passed in by REST service
NON_INTERACTIVE="${ALBERT_NONINTERACTIVE:-}"     # suppress prompts

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
			*) POST_FLAGS+=("$1"); shift ;;
		esac
	done
	set -- "$CMD" "${POST_FLAGS[@]}"
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

# --- API Key validation ----------------------------------------------------
API_KEY_DB_ID=""
require_api_key() {
	if [ -z "$OWNER_KEY_HASH_ENV" ]; then
		json_error 2 "API key required" "This operation requires an API key. Use --api-key <PLAINTEXT> or --api-key-hash <HASH>."
	fi
	if [ ! -f "$DB_PATH" ]; then
		json_error 2 "DB missing" "Manager DB not found at $DB_PATH – cannot validate API key. Install or create key first."
	fi
	# Look up api key row id
	API_KEY_DB_ID=$(sqlite3 "$DB_PATH" "SELECT id FROM api_keys WHERE key_hash='$OWNER_KEY_HASH_ENV' LIMIT 1;" 2>/dev/null || true)
	if [ -z "$API_KEY_DB_ID" ]; then
		json_error 2 "Unknown API key" "Provided API key hash not registered. Register the key first."
	fi
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

# Create container
create_container() {
	local name=$1

	# Require valid API key (must exist in DB)
	require_api_key
	
	# If no name provided, generate cryptic name
	if [ -z "$name" ]; then
		name=$(generate_cryptic_name)
		echo -e "${BLUE}Generating cryptic container name: ${name}${NC}"
	fi
	
	# Check if container already exists
	if docker ps -a --format '{{.Names}}' | grep -q "^${name}$"; then
		json_error 1 "Exists" "Container '$name' already exists"
	fi
	
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
