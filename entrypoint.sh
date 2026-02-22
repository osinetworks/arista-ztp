#!/bin/bash
# ============================================================
# entrypoint.sh - ZTP Server container startup
# ============================================================

set -e

ZTP_BASE_DIR="${ZTP_BASE_DIR:-/var/www/ztp}"
LOG_DIR="${LOG_DIR:-/var/www/ztp/logs}"
INVENTORY_PATH="${INVENTORY_PATH:-/var/www/ztp/config/inventory.yaml}"
ZTP_PORT="${ZTP_PORT:-8080}"
PRIORITY_WAIT_TIMEOUT="${PRIORITY_WAIT_TIMEOUT:-1800}"

# 1 worker + threads: all threads share the same memory so completed_serials
# and ztp_events are consistent across all requests. Multiple workers would
# create isolated processes that cannot see each other's state, causing
# priority gating to deadlock (ztp_complete hits worker A, manifest check
# hits worker B which has empty state and waits forever).
ZTP_WORKERS="${ZTP_WORKERS:-1}"
ZTP_THREADS="${ZTP_THREADS:-16}"

# Standard gunicorn timeout — manifest endpoint now returns instantly (202)
# instead of blocking, so no long timeout is needed.
GUNICORN_TIMEOUT=60

echo "=========================================="
echo " Multi-Vendor ZTP Server"
echo "=========================================="
echo " Base dir         : $ZTP_BASE_DIR"
echo " Inventory        : $INVENTORY_PATH"
echo " Log dir          : $LOG_DIR"
echo " Port             : $ZTP_PORT"
echo " Workers          : $ZTP_WORKERS (1 worker + $ZTP_THREADS threads)"
echo " Gunicorn timeout : ${GUNICORN_TIMEOUT}s (manifest polls from bootstrap, no long holds)"
echo " Priority timeout : ${PRIORITY_WAIT_TIMEOUT}s"
echo "=========================================="

# Ensure required directories exist
mkdir -p "$ZTP_BASE_DIR/configs"
mkdir -p "$ZTP_BASE_DIR/firmware"
mkdir -p "$ZTP_BASE_DIR/logs"
mkdir -p "$ZTP_BASE_DIR/config"
mkdir -p /var/lib/dnsmasq

# Copy default inventory if not present
if [ ! -f "$INVENTORY_PATH" ]; then
    if [ -f "/etc/ztp/inventory.yaml" ]; then
        cp /etc/ztp/inventory.yaml "$INVENTORY_PATH"
        echo "[entrypoint] Copied default inventory to $INVENTORY_PATH"
    else
        echo "[entrypoint] WARNING: No inventory file found at $INVENTORY_PATH"
    fi
fi

# -----------------------------------------------------------
# Auto-detect network interface for dnsmasq
# -----------------------------------------------------------
if [ -n "${ZTP_INTERFACE:-}" ]; then
    IFACE="$ZTP_INTERFACE"
    echo "[entrypoint] Using interface from ZTP_INTERFACE env: $IFACE"
else
    IFACE=$(ip -o link show | awk -F': ' '$2 != "lo" && $3 ~ /UP/ {print $2; exit}')
    if [ -z "$IFACE" ]; then
        IFACE=$(ip -o link show | awk -F': ' '$2 != "lo" {print $2; exit}')
    fi
    echo "[entrypoint] Auto-detected interface: $IFACE"
fi

if [ -z "$IFACE" ]; then
    echo "[entrypoint] ERROR: Could not detect a network interface. Set ZTP_INTERFACE env var."
    exit 1
fi

# Write interface to writable override (avoids touching the bind-mounted dnsmasq.conf)
DNSMASQ_OVERRIDE="/tmp/dnsmasq-interface.conf"
echo "interface=${IFACE}" > "$DNSMASQ_OVERRIDE"
echo "[entrypoint] dnsmasq interface set to: $IFACE"

# -----------------------------------------------------------
# Start dnsmasq
# -----------------------------------------------------------
echo "[entrypoint] Starting dnsmasq..."
dnsmasq \
    --no-daemon \
    --log-dhcp \
    --log-queries \
    --log-facility=- \
    --conf-file=/etc/dnsmasq.conf \
    --conf-file="$DNSMASQ_OVERRIDE" &
DNSMASQ_PID=$!

# -----------------------------------------------------------
# Start gunicorn (production WSGI server)
# -----------------------------------------------------------
echo "[entrypoint] Starting ZTP server via gunicorn..."
export PYTHONPATH="/usr/local/bin:${PYTHONPATH:-}"

gunicorn \
    --workers "$ZTP_WORKERS" \
    --threads "$ZTP_THREADS" \
    --bind "0.0.0.0:${ZTP_PORT}" \
    --chdir /usr/local/bin \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    --timeout "$GUNICORN_TIMEOUT" \
    ztp_server:app &
GUNICORN_PID=$!

echo "[entrypoint] Services started (dnsmasq PID=$DNSMASQ_PID, gunicorn PID=$GUNICORN_PID)"

# Graceful shutdown on SIGTERM/SIGINT
trap "echo '[entrypoint] Shutting down...'; kill $DNSMASQ_PID $GUNICORN_PID 2>/dev/null; exit 0" SIGTERM SIGINT

# Wait for either process to exit
wait -n 2>/dev/null || wait
echo "[entrypoint] A service exited, shutting down."
kill $DNSMASQ_PID $GUNICORN_PID 2>/dev/null
exit 1