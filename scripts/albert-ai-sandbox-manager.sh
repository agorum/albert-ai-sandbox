#!/bin/bash

source /opt/albert-ai-sandbox-manager/scripts/common.sh
source /opt/albert-ai-sandbox-manager/scripts/port-manager.sh
source /opt/albert-ai-sandbox-manager/scripts/nginx-manager.sh

DOCKER_IMAGE="albert-ai-sandbox:latest"

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

# Create container
create_container() {
	local name=$1
	
	# If no name provided, generate cryptic name
	if [ -z "$name" ]; then
		name=$(generate_cryptic_name)
		echo -e "${BLUE}Generating cryptic container name: ${name}${NC}"
	fi
	
	# Check if container already exists
	if docker ps -a --format '{{.Names}}' | grep -q "^${name}$"; then
		echo -e "${RED}Error: Container '$name' already exists${NC}"
		return 1
	fi
	
	# Find free ports
	local novnc_port=$(find_free_novnc_port)
	local vnc_port=$(find_free_vnc_port)
	local mcphub_port=$(find_free_mcphub_port)
	
	if [ -z "$novnc_port" ] || [ -z "$vnc_port" ] || [ -z "$mcphub_port" ]; then
		echo -e "${RED}Error: No free ports available${NC}"
		return 1
	fi
	
	echo -e "${YELLOW}Creating sandbox container '$name'...${NC}"
	echo -e "${BLUE}  noVNC Port: $novnc_port${NC}"
	echo -e "${BLUE}  VNC Port: $vnc_port${NC}"
	echo -e "${BLUE}  MCP Hub Port: $mcphub_port${NC}"
	
	# Create Docker container
	docker run -d \
		--name "$name" \
		--restart unless-stopped \
		--cap-add=SYS_ADMIN \
		--security-opt seccomp=unconfined \
		-p ${novnc_port}:6081 \
		-p ${vnc_port}:5901 \
		-p ${mcphub_port}:3000 \
		-e VNC_PORT=5901 \
		-e NO_VNC_PORT=6081 \
		-e MCP_HUB_PORT=3000 \
		-v ${name}_data:/home/ubuntu \
		--shm-size=2g \
		$DOCKER_IMAGE
	
	if [ $? -eq 0 ]; then
		# Register in registry
		add_to_registry "$name" "$novnc_port" "$vnc_port" "$mcphub_port"
		
		# Configure nginx
		create_nginx_config "$name" "$novnc_port" "$mcphub_port"
		
		# Create global MCP Hub configuration (only once)
		if [ ! -f "${NGINX_CONF_DIR}/albert-mcphub-global.conf" ]; then
			create_global_mcphub_config "$mcphub_port"
		fi
		
		echo -e "${GREEN}========================================${NC}"
		echo -e "${GREEN}Sandbox container created successfully!${NC}"
		echo -e "${GREEN}========================================${NC}"
		echo -e "${GREEN}Name: ${name}${NC}"
		echo -e "${GREEN}URL: http://$(hostname -I | awk '{print $1}')/${name}/${NC}"
		echo -e "${GREEN}MCP Hub: http://$(hostname -I | awk '{print $1}')/${name}/mcphub/${NC}"
		echo -e "${YELLOW}MCP Hub Login: admin / albert${NC}"
		echo -e "${YELLOW}MCP Hub Bearer token: albert${NC}"
		echo -e "${YELLOW}Important: Note the URL - the name is the access protection!${NC}"
	else
		echo -e "${RED}Error creating container${NC}"
		return 1
	fi
}

# Remove container
remove_container() {
	local name=$1
	
	if [ -z "$name" ]; then
		echo -e "${RED}Error: Container name required${NC}"
		return 1
	fi
	
	echo -e "${YELLOW}Removing container '$name'...${NC}"
	
	# Stop and remove container
	docker stop "$name" 2>/dev/null
	docker rm "$name" 2>/dev/null
	
	# Remove volume (optional)
	read -p "Also delete data volume? (y/n): " -n 1 -r
	echo
	if [[ $REPLY =~ ^[Yy]$ ]]; then
			docker volume rm "${name}_data" 2>/dev/null
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
	
	if [ -z "$name" ]; then
		echo -e "${RED}Error: Container name required${NC}"
		return 1
	fi
	
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
	
	if [ -z "$name" ]; then
		echo -e "${RED}Error: Container name required${NC}"
		return 1
	fi
	
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
	
	if [ -z "$name" ]; then
		echo -e "${RED}Error: Container name required${NC}"
		return 1
	fi
	
	stop_container "$name"
	start_container "$name"
}

# Show status
show_status() {
	local name=$1
	
	if [ -z "$name" ]; then
		# Show all containers
		echo -e "${GREEN}Status of all sandbox containers:${NC}"
		echo -e "${GREEN}=================================${NC}"
		echo -e "${BLUE}Desktop: KDE Plasma${NC}"
		echo "------------------------------"
		
		for container_name in $(get_all_containers); do
			show_single_status "$container_name"
			echo "------------------------------"
		done
	else
		show_single_status "$name"
	fi
}

# Show single container status
show_single_status() {
	local name=$1
	local info=$(get_container_info "$name")
	
	if [ -z "$info" ]; then
			echo -e "${RED}Container '$name' not found in registry${NC}"
			return 1
	fi
	
	local port=$(echo "$info" | jq -r '.port')
	local vnc_port=$(echo "$info" | jq -r '.vnc_port')
	local created=$(echo "$info" | jq -r '.created')
	
	echo -e "${BLUE}Container: ${NC}$name"
	echo -e "${BLUE}Created: ${NC}$created"
	echo -e "${BLUE}Desktop: ${NC}KDE Plasma"
	echo -e "${BLUE}noVNC Port: ${NC}$port"
	echo -e "${BLUE}VNC Port: ${NC}$vnc_port"
	
	# Docker Status
	if docker ps --format '{{.Names}}' | grep -q "^${name}$"; then
		echo -e "${BLUE}Docker Status: ${GREEN}Running${NC}"
		local stats=$(docker stats --no-stream --format "CPU: {{.CPUPerc}} | RAM: {{.MemUsage}}" "$name" 2>/dev/null)
		echo -e "${BLUE}Resources: ${NC}$stats"
	else
		echo -e "${BLUE}Docker Status: ${RED}Stopped${NC}"
	fi
	
	echo -e "${BLUE}URL: ${NC}http://$(hostname -I | awk '{print $1}')/${name}/"
}

# List containers
list_containers() {
	echo -e "${GREEN}ALBERT Sandbox Containers:${NC}"
	echo -e "${GREEN}========================================${NC}"
	
	printf "%-30s %-10s %-10s %-10s\n" "NAME" "STATUS" "NOVNC-PORT" "VNC-PORT"
	printf "%-30s %-10s %-10s %-10s\n" "----" "------" "----------" "--------"
	
	for container_name in $(get_all_containers); do
		local info=$(get_container_info "$container_name")
		local port=$(echo "$info" | jq -r '.port')
		local vnc_port=$(echo "$info" | jq -r '.vnc_port')
		
		if docker ps --format '{{.Names}}' | grep -q "^${container_name}$"; then
				local status="${GREEN}Running${NC}"
		else
				local status="${RED}Stopped${NC}"
		fi
		
		printf "%-30s %-20b %-10s %-10s\n" "$container_name" "$status" "$port" "$vnc_port"
	done
	
	echo ""
	echo -e "${BLUE}Desktop: KDE Plasma | VNC Password: albert${NC}"
	echo ""
	echo -e "${BLUE}Access URLs:${NC}"
	for container_name in $(get_all_containers); do
		echo "  http://$(hostname -I | awk '{print $1}')/${container_name}/"
	done
}

# Main program
case "$1" in
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
		echo -e "${RED}Unknown command: $1${NC}"
		show_help
		exit 1
		;;
esac
