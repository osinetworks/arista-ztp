#!/bin/bash
# ============================================================
# start.sh - ZTP Docker Management Script
# ============================================================

set -uo pipefail

IMAGE_NAME="ztp-server"
CONTAINER_NAME="ztp-server"
COMPOSE_FILE="docker-compose.yaml"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------

log()     { echo -e "${GREEN}[ZTP]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
section() { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }

usage() {
    echo -e "
${BOLD}Multi-Vendor ZTP - Docker Management Script${NC}

Usage:
  ${BOLD}./start.sh${NC} [command]

Commands:
  ${GREEN}build${NC}       Build the Docker image
  ${GREEN}start${NC}       Start ZTP server (build if needed)
  ${GREEN}stop${NC}        Stop and remove the container
  ${GREEN}restart${NC}     Stop, rebuild, and start
  ${GREEN}logs${NC}        Follow container logs
  ${GREEN}status${NC}      Show container and service status
  ${GREEN}priority${NC}    Show provisioning priority status
  ${GREEN}shell${NC}       Open a shell inside the container
  ${GREEN}reload${NC}      Hot-reload inventory without restart
  ${GREEN}switches${NC}    List all switches in inventory
  ${GREEN}events${NC}      Show recent ZTP events
  ${GREEN}watch${NC}       Live progress dashboard (auto-refresh)
  ${GREEN}clean${NC}       Remove container and image
  ${GREEN}help${NC}        Show this help message

Examples:
  ./start.sh start
  ./start.sh logs
  ./start.sh priority
  ./start.sh events
"
    exit 0
}

# -----------------------------------------------------------
# Prerequisite checks
# -----------------------------------------------------------

check_deps() {
    local missing=0
    if ! command -v docker &>/dev/null; then
        error "docker not found — please install Docker"
        missing=1
    fi

    if docker compose version &>/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        error "Docker Compose not found (install Docker Compose v2 or docker-compose v1)"
        missing=1
    fi

    if [ "$missing" -eq 1 ]; then
        exit 1
    fi
}

check_env() {
    if [ ! -f "config/inventory.yaml" ]; then
        warn "config/inventory.yaml not found — switches will use defaults only"
    fi

    if [ ! -f "config/dnsmasq.conf" ]; then
        warn "config/dnsmasq.conf not found — DHCP will not work"
    fi

    local fw_count=0
    fw_count=$(find firmware/ -name "*.swi" -o -name "*.bin" 2>/dev/null | wc -l) || fw_count=0
    if [ "$fw_count" -eq 0 ]; then
        warn "No firmware files found in firmware/ — switches cannot download firmware"
    else
        log "Found $fw_count firmware image(s) in firmware/"
    fi

    local cfg_count=0
    cfg_count=$(find configs/ -name "*.cfg" 2>/dev/null | wc -l) || cfg_count=0
    log "Found $cfg_count config file(s) in configs/"
}

# -----------------------------------------------------------
# Commands
# -----------------------------------------------------------

cmd_build() {
    section "Building Docker Image"
    $COMPOSE_CMD -f "$COMPOSE_FILE" build --no-cache
    log "Build complete: ${IMAGE_NAME}:latest"
}

cmd_start() {
    section "Starting ZTP Server"
    check_env

    mkdir -p logs configs firmware

    # Build image if it doesn't exist
    if ! docker image inspect "${IMAGE_NAME}:latest" &>/dev/null; then
        log "Image '${IMAGE_NAME}:latest' not found — building first..."
        cmd_build
    else
        log "Image '${IMAGE_NAME}:latest' found — skipping build (use './start.sh restart' to rebuild)"
    fi

    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d
    if [ $? -ne 0 ]; then
        error "Failed to start container — run './start.sh logs' for details"
        exit 1
    fi
    log "Container started: $CONTAINER_NAME"

    # Wait for health check
    echo -ne "${CYAN}[ZTP]${NC} Waiting for server to be ready"
    local ready=0
    for i in $(seq 1 15); do
        sleep 2
        if curl -sf "http://localhost:8080/health" &>/dev/null; then
            ready=1
            break
        fi
        echo -n "."
    done
    echo ""

    if [ "$ready" -eq 1 ]; then
        log "ZTP server is healthy ✓"
        cmd_status
    else
        warn "Server did not respond on :8080 — check logs: ./start.sh logs"
    fi
}

cmd_stop() {
    section "Stopping ZTP Server"
    # --remove-orphans cleans up containers from old compose projects
    # (e.g. if container_name changed from arista-ztp to ztp-server)
    $COMPOSE_CMD -f "$COMPOSE_FILE" down --remove-orphans

    # Also force-remove any leftover container with the old name
    if docker ps -a --format '{{.Names}}' | grep -q "^arista-ztp$"; then
        warn "Removing leftover container 'arista-ztp' from previous version..."
        docker rm -f arista-ztp 2>/dev/null || true
    fi

    log "Container stopped."
}

cmd_restart() {
    section "Restarting ZTP Server"
    cmd_stop
    cmd_build
    cmd_start
}

cmd_logs() {
    section "Container Logs (Ctrl+C to exit)"
    $COMPOSE_CMD -f "$COMPOSE_FILE" logs -f --tail=100
}

cmd_status() {
    section "ZTP Server Status"

    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        local started
        started=$(docker inspect --format '{{.State.StartedAt}}' "$CONTAINER_NAME" 2>/dev/null || echo "unknown")
        echo -e "  Container : ${GREEN}RUNNING${NC} (started $started)"
    else
        echo -e "  Container : ${RED}NOT RUNNING${NC}"
        return
    fi

    if curl -sf "http://localhost:8080/health" &>/dev/null; then
        local health sw_count events_count
        health=$(curl -s "http://localhost:8080/health")
        sw_count=$(echo "$health"     | python3 -c "import sys,json; print(json.load(sys.stdin).get('switches','?'))" 2>/dev/null || echo "?")
        events_count=$(echo "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('events_recorded','?'))" 2>/dev/null || echo "?")
        completed=$(echo "$health"    | python3 -c "import sys,json; c=json.load(sys.stdin).get('completed',[]); print(', '.join(c) if c else 'none')" 2>/dev/null || echo "?")
        echo -e "  HTTP API  : ${GREEN}OK${NC} — $sw_count switch(es) in inventory, $events_count event(s) recorded"
        echo -e "  Completed : $completed"
    else
        echo -e "  HTTP API  : ${RED}NOT RESPONDING${NC} on :8080"
    fi

    local fw_count=0 cfg_count=0
    fw_count=$(find firmware/ \( -name "*.swi" -o -name "*.bin" \) 2>/dev/null | wc -l) || true
    cfg_count=$(find configs/ -name "*.cfg" 2>/dev/null | wc -l) || true

    echo -e "  Firmware  : $fw_count image(s) available"
    echo -e "  Configs   : $cfg_count .cfg file(s) available"
    echo ""
    echo -e "  ${CYAN}Endpoints:${NC}"
    echo -e "    Bootstrap (Arista) : http://localhost:8080/bootstrap/arista"
    echo -e "    Bootstrap (Cisco)  : http://localhost:8080/bootstrap/cisco"
    echo -e "    Health             : http://localhost:8080/health"
    echo -e "    Priority status    : http://localhost:8080/api/status"
    echo -e "    Switches           : http://localhost:8080/api/switches"
    echo -e "    Events             : http://localhost:8080/api/events"
}

cmd_priority() {
    section "Provisioning Priority Status"
    local data
    data=$(curl -sf "http://localhost:8080/api/status" 2>/dev/null) || true
    if [ -z "$data" ]; then
        error "Server not reachable — is the container running?"
        exit 1
    fi

    echo "$data" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f\"Completed: {data.get('completed_count',0)}/{data.get('total_switches',0)}\")
print()
for group in data.get('provisioning_order', []):
    p = group['priority']
    label = group['label']
    done = '✓' if group['all_complete'] else '...'
    print(f'  [{done}] {label}')
    for sw in group['switches']:
        status = '✓ done' if sw['completed'] else ('▶ go' if sw['clear_to_go'] else f'⏳ {sw[\"waiting_for\"]}')
        print(f'       {sw[\"serial\"]:20} {sw[\"description\"]:15} {sw[\"vendor\"]:8} {status}')
    print()
"
}

cmd_shell() {
    section "Opening Shell in Container"
    docker exec -it "$CONTAINER_NAME" /bin/bash 2>/dev/null || docker exec -it "$CONTAINER_NAME" /bin/sh
}

cmd_reload() {
    section "Hot-Reloading Inventory"
    local result
    result=$(curl -sf -X POST "http://localhost:8080/api/inventory/reload" 2>/dev/null) || true
    if [ -n "$result" ]; then
        log "Inventory reloaded: $result"
    else
        error "Server not reachable on :8080 — is the container running?"
        exit 1
    fi
}

cmd_switches() {
    section "Registered Switches"
    local data
    data=$(curl -sf "http://localhost:8080/api/switches" 2>/dev/null) || true
    if [ -z "$data" ]; then
        error "Server not reachable — is the container running?"
        exit 1
    fi

    if [ "$data" = "[]" ]; then
        warn "No switches registered in inventory."
        return
    fi

    printf "%-22s %-15s %-10s %-8s %-25s %-30s\n" "SERIAL" "DESCRIPTION" "PRIORITY" "VENDOR" "CONFIG" "FIRMWARE"
    printf '%0.s─' {1..115}; echo ""
    echo "$data" | python3 -c "
import sys, json
for sw in json.load(sys.stdin):
    vendor = 'cisco' if sw.get('platform') == 'cisco_ios' else 'arista'
    print('{:<22} {:<15} {:<10} {:<8} {:<25} {}'.format(
        sw.get('serial',''), sw.get('description','')[:13],
        sw.get('priority', 99), vendor,
        sw.get('config',''), sw.get('firmware',''),
    ))
"
}

cmd_events() {
    section "Recent ZTP Events"
    local data
    data=$(curl -sf "http://localhost:8080/api/events" 2>/dev/null) || true
    if [ -z "$data" ]; then
        error "Server not reachable — is the container running?"
        exit 1
    fi

    if [ "$data" = "[]" ]; then
        warn "No ZTP events recorded yet."
        return
    fi

    echo "$data" | python3 -c "
import sys, json
for e in json.load(sys.stdin)[-30:]:
    print('[{}] {:20} {:25} {}'.format(
        e.get('timestamp',''), e.get('serial',''),
        e.get('event',''), str(e.get('detail',''))[:50],
    ))
"
}

cmd_watch() {
    section "Live ZTP Progress Dashboard"
    echo -e "  Refreshing every 5s — ${YELLOW}Ctrl+C to exit${NC}
"

    while true; do
        # Move cursor to top, clear screen
        clear
        echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════════${NC}"
        echo -e "${BOLD}  Multi-Vendor ZTP — Live Progress$(date +'  %H:%M:%S')${NC}"
        echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════════${NC}"

        local data
        data=$(curl -sf "http://localhost:8080/api/progress" 2>/dev/null) || true

        if [ -z "$data" ]; then
            echo -e "  ${RED}Server not reachable on :8080${NC}"
        else
            echo "$data" | python3 -c "
import sys, json

RESET  = '[0m'
BOLD   = '[1m'
GREEN  = '[0;32m'
YELLOW = '[1;33m'
RED    = '[0;31m'
CYAN   = '[0;36m'
GRAY   = '[0;37m'

BAR_WIDTH = 30

def bar(pct, width=BAR_WIDTH):
    filled = int(width * pct / 100)
    return GREEN + '█' * filled + GRAY + '░' * (width - filled) + RESET

def fmt_bytes(b):
    if b >= 1024*1024*1024:
        return f'{b/1024/1024/1024:.1f}GB'
    elif b >= 1024*1024:
        return f'{b/1024/1024:.1f}MB'
    elif b >= 1024:
        return f'{b/1024:.1f}KB'
    return f'{b}B'

switches = json.load(sys.stdin)
total = len(switches)
done  = sum(1 for s in switches if s.get('completed'))
print(f'  Switches: {GREEN}{done}{RESET}/{total} complete')
print()

last_priority = None
for sw in switches:
    priority = sw.get('priority', 99)
    if priority != last_priority:
        print(f'  {CYAN}Priority {priority}{RESET}')
        last_priority = priority

    serial  = sw.get('serial','')
    desc    = sw.get('description','')
    vendor  = sw.get('vendor','')
    pct     = sw.get('pct', 0)
    msg     = sw.get('msg', '')
    step    = sw.get('step', '')
    brecv   = sw.get('bytes_received', 0)
    btotal  = sw.get('bytes_total', 0)
    compl   = sw.get('completed', False)

    # Status icon
    if compl:
        icon = GREEN + '✓' + RESET
    elif step == 'failed':
        icon = RED + '✗' + RESET
    elif step == 'not_started':
        icon = GRAY + '○' + RESET
    else:
        icon = YELLOW + '▶' + RESET

    vcolor = CYAN if vendor == 'arista' else YELLOW
    print(f'  {icon} {BOLD}{serial}{RESET}  {vcolor}{vendor:8}{RESET} {desc}')

    if step not in ('not_started', 'done', 'failed', ''):
        # Progress bar (only meaningful during download)
        if btotal > 0:
            print(f'      [{bar(pct)}] {pct:3d}%  {fmt_bytes(brecv)}/{fmt_bytes(btotal)}')
        else:
            print(f'      [{bar(pct)}] {pct:3d}%')

    # Status message
    msg_color = RED if 'FAIL' in msg.upper() or 'ERROR' in msg.upper() else                 GREEN if compl else GRAY
    print(f'      {msg_color}{msg}{RESET}')
    print()
"
        fi

        sleep 5
    done
}

cmd_clean() {
    section "Cleaning Up"
    warn "This will remove the container and Docker image."
    read -rp "Are you sure? [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        $COMPOSE_CMD -f "$COMPOSE_FILE" down --rmi local --volumes --remove-orphans 2>/dev/null || true
        docker rm -f arista-ztp 2>/dev/null || true
        docker rmi "${IMAGE_NAME}:latest" 2>/dev/null || true
        log "Cleanup complete."
    else
        log "Aborted."
    fi
}

# -----------------------------------------------------------
# Entry point
# -----------------------------------------------------------

check_deps

COMMAND="${1:-help}"

case "$COMMAND" in
    build)    cmd_build    ;;
    start)    cmd_start    ;;
    stop)     cmd_stop     ;;
    restart)  cmd_restart  ;;
    logs)     cmd_logs     ;;
    status)   cmd_status   ;;
    priority) cmd_priority ;;
    shell)    cmd_shell    ;;
    reload)   cmd_reload   ;;
    switches) cmd_switches ;;
    events)   cmd_events   ;;
    watch)    cmd_watch    ;;
    clean)    cmd_clean    ;;
    help|--help|-h) usage  ;;
    *)
        error "Unknown command: $COMMAND"
        usage
        ;;
esac