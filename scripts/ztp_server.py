#!/usr/bin/env python3
# ============================================================
# ztp_server.py
# Flask-based ZTP HTTP server.
# Serves bootstrap, configs, firmware.
# Provides REST API for manifest lookup, notifications, mgmt.
# Supports priority-based provisioning order.
# ============================================================

import os
import json
import logging
import datetime
import time
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, abort

from inventory_manager import InventoryManager

# -----------------------------------------------------------
# Configuration from environment
# -----------------------------------------------------------
HOST            = os.environ.get("ZTP_HOST",          "0.0.0.0")
PORT            = int(os.environ.get("ZTP_PORT",      "8080"))
SERVER_IP       = os.environ.get("ZTP_SERVER_IP",    "192.168.100.1")
BASE_DIR        = os.environ.get("ZTP_BASE_DIR",      "/var/www/ztp")
INVENTORY_PATH  = os.environ.get("INVENTORY_PATH",    "/var/www/ztp/config/inventory.yaml")
LOG_LEVEL       = os.environ.get("LOG_LEVEL",         "INFO")
LOG_DIR         = os.environ.get("LOG_DIR",           "/var/www/ztp/logs")

# Max seconds to wait for lower-priority switches before releasing manifest anyway
PRIORITY_WAIT_TIMEOUT = int(os.environ.get("PRIORITY_WAIT_TIMEOUT", "1800"))  # 30 min
# How often to re-check while waiting
PRIORITY_POLL_INTERVAL = 15  # seconds

# -----------------------------------------------------------
# Logging setup
# -----------------------------------------------------------
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "ztp_server.log")),
    ]
)
logger = logging.getLogger("ztp.server")

# -----------------------------------------------------------
# Flask app
# -----------------------------------------------------------
app = Flask(__name__)
inventory = InventoryManager(INVENTORY_PATH)

# In-memory ZTP event log
ztp_events = []

# Priority state tracker: serial → True if ztp_complete received
completed_serials: dict = {}

# Progress tracker: serial → progress dict
# {
#   "step": "firmware_download" | "firmware_install" | "config" | "done",
#   "pct":  0-100,
#   "msg":  "human readable status",
#   "bytes_received": int,
#   "bytes_total": int,
#   "updated": timestamp
# }
progress_state: dict = {}


def ts():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def is_priority_clear(serial: str) -> tuple[bool, str]:
    """
    Check if all switches with LOWER priority number than this serial
    have reported ztp_complete.
    Returns (True, "") if clear to proceed, or (False, reason) if waiting.
    """
    my_priority = inventory.get_priority(serial)

    # Collect all priorities lower than mine
    blocking_priorities = [p for p in inventory.get_all_priorities() if p < my_priority]

    if not blocking_priorities:
        return True, ""  # no lower priority switches — proceed immediately

    # For each lower priority, check all its serials are complete
    for priority in blocking_priorities:
        serials_at_priority = inventory.get_serials_with_priority(priority)
        for s in serials_at_priority:
            if not completed_serials.get(s, False):
                return False, f"waiting for priority {priority} switch {s} to complete"

    return True, ""


# -----------------------------------------------------------
# File serving endpoints
# -----------------------------------------------------------

def _inject_server_config(script_path: str) -> str:
    """
    Read bootstrap script and replace __ZTP_SERVER_IP__ / __ZTP_SERVER_PORT__
    placeholders with the actual values from environment.
    This avoids hardcoding the server IP in scripts — change .env, rebuild,
    and the new IP is automatically sent to every switch.
    """
    with open(script_path, "r") as f:
        content = f.read()
    content = content.replace("__ZTP_SERVER_IP__",   SERVER_IP)
    content = content.replace("__ZTP_SERVER_PORT__", str(PORT))
    return content


@app.route("/bootstrap/arista", methods=["GET"])
@app.route("/bootstrap",         methods=["GET"])  # legacy compatibility
def serve_bootstrap_arista():
    """Serve the Arista EOS bootstrap script with server IP/port injected."""
    client_ip     = request.remote_addr
    arista_serial = request.headers.get("X-Arista-Serial", "")
    arista_sku    = request.headers.get("X-Arista-SKU", "")
    logger.info(f"[ARISTA] Bootstrap requested by {client_ip} serial={arista_serial} sku={arista_sku}")
    script = _inject_server_config(os.path.join(BASE_DIR, "bootstrap_arista"))
    return script, 200, {"Content-Type": "text/plain"}


@app.route("/bootstrap/cisco", methods=["GET"])
def serve_bootstrap_cisco():
    """Serve the Cisco IOS XE bootstrap script with server IP/port injected."""
    client_ip = request.remote_addr
    logger.info(f"[CISCO] Bootstrap requested by {client_ip}")
    script = _inject_server_config(os.path.join(BASE_DIR, "bootstrap_cisco"))
    return script, 200, {"Content-Type": "text/plain"}


@app.route("/configs/<path:filename>", methods=["GET"])
def serve_config(filename):
    """Serve per-switch or generic configuration files."""
    config_dir = os.path.join(BASE_DIR, "configs")
    filepath = os.path.join(config_dir, filename)
    if not os.path.isfile(filepath):
        logger.warning(f"Config not found: {filename} (requested by {request.remote_addr})")
        abort(404)
    logger.info(f"Serving config: {filename} to {request.remote_addr}")
    return send_from_directory(config_dir, filename, mimetype="text/plain")


@app.route("/firmware/<path:filename>", methods=["GET"])
def serve_firmware(filename):
    """
    Serve firmware images with live byte-level progress tracking.
    Streams the file in chunks and updates progress_state so the
    /api/progress/<serial> endpoint can report download percentage.
    Serial is looked up by matching the requesting IP to a known switch.
    """
    from flask import Response, stream_with_context
    firmware_dir = os.path.join(BASE_DIR, "firmware")
    filepath = os.path.join(firmware_dir, filename)
    if not os.path.isfile(filepath):
        logger.warning(f"Firmware not found: {filename} (requested by {request.remote_addr})")
        abort(404)

    total_bytes = os.path.getsize(filepath)
    client_ip   = request.remote_addr

    # Try to find which serial this IP belongs to (from recent events)
    serial = "UNKNOWN"
    for e in reversed(ztp_events):
        if e.get("client_ip") == client_ip and e.get("serial"):
            serial = e["serial"]
            break

    logger.info(f"Serving firmware: {filename} ({total_bytes/1024/1024:.1f}MB) to {client_ip} (serial={serial})")

    # Init progress
    progress_state[serial] = {
        "step":           "firmware_download",
        "pct":            0,
        "msg":            f"Downloading {filename}",
        "bytes_received": 0,
        "bytes_total":    total_bytes,
        "filename":       filename,
        "updated":        ts(),
    }

    CHUNK = 1024 * 1024  # 1MB chunks

    def generate():
        sent = 0
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                yield chunk
                sent += len(chunk)
                pct = int(sent * 100 / total_bytes) if total_bytes else 100
                progress_state[serial].update({
                    "pct":            pct,
                    "bytes_received": sent,
                    "msg":            f"Downloading {filename} — {sent/1024/1024:.1f}/{total_bytes/1024/1024:.1f} MB ({pct}%)",
                    "updated":        ts(),
                })
        # Mark download complete
        progress_state[serial].update({
            "step":    "firmware_install",
            "pct":     100,
            "msg":     f"Download complete — waiting for install to begin",
            "updated": ts(),
        })
        logger.info(f"Firmware download complete for {serial}: {filename}")

    return Response(
        stream_with_context(generate()),
        mimetype="application/octet-stream",
        headers={"Content-Length": str(total_bytes)},
    )


# -----------------------------------------------------------
# API: Manifest — with priority gating
# -----------------------------------------------------------

@app.route("/api/manifest/<serial>", methods=["GET"])
def api_manifest(serial):
    """
    Return JSON manifest for a given switch serial.
    Called by the bootstrap script running on the switch.

    Priority gating — NON-BLOCKING design:
    - If this switch is clear to proceed → return 200 with manifest
    - If this switch must wait for lower-priority switches → return 202
      with {"status": "waiting", "reason": "..."}
    - The bootstrap script polls this endpoint every PRIORITY_POLL_INTERVAL
      seconds until it receives a 200. This keeps gunicorn workers free
      to handle other requests (firmware downloads, notifications) while
      switches are waiting.
    """
    serial = serial.upper().strip()
    manifest = inventory.get_manifest(serial)
    my_priority = manifest.get("priority", 99)

    logger.info(f"Manifest request: {serial} priority={my_priority} source={manifest['source']}")

    # --- Non-blocking priority gate ---
    clear, reason = is_priority_clear(serial)
    if not clear:
        logger.info(f"Manifest deferred for {serial} (priority {my_priority}): {reason}")
        return jsonify({
            "status":   "waiting",
            "serial":   serial,
            "priority": my_priority,
            "reason":   reason,
        }), 202

    # Clear to proceed — build and return full manifest
    platform = manifest.get("platform", "eos")
    if platform == "cisco_ios":
        manifest["bootstrap_url"] = f"http://{SERVER_IP}:{PORT}/bootstrap/cisco"
        manifest["vendor"] = "cisco"
    else:
        manifest["bootstrap_url"] = f"http://{SERVER_IP}:{PORT}/bootstrap/arista"
        manifest["vendor"] = "arista"

    logger.info(f"Manifest released: {serial} → vendor={manifest['vendor']} config={manifest['config']} firmware={manifest['firmware']} priority={my_priority}")

    ztp_events.append({
        "timestamp": ts(),
        "serial":    serial,
        "event":     "manifest_served",
        "detail":    manifest,
        "client_ip": request.remote_addr,
    })

    return jsonify(manifest), 200


# -----------------------------------------------------------
# API: Notifications (from bootstrap on switch)
# -----------------------------------------------------------

@app.route("/api/notify", methods=["POST"])
def api_notify():
    """
    Receive ZTP status events from switches.
    When event=ztp_complete is received, mark serial as done
    so higher-priority switches waiting in api_manifest are unblocked.
    """
    data   = request.get_json(silent=True) or {}
    serial = data.get("serial", "UNKNOWN").upper()
    event  = data.get("event",  "unknown")
    detail = data.get("detail", "")

    entry = {
        "timestamp": ts(),
        "serial":    serial,
        "event":     event,
        "detail":    detail,
        "client_ip": request.remote_addr,
    }
    ztp_events.append(entry)

    # Update progress state based on event
    if event == "ztp_started":
        progress_state[serial] = {
            "step": "starting", "pct": 0,
            "msg": "ZTP started — fetching manifest",
            "bytes_received": 0, "bytes_total": 0,
            "filename": "", "updated": ts(),
        }
    elif event == "config_applied":
        progress_state[serial] = {
            "step": "config_done", "pct": 0,
            "msg": f"Config applied: {detail}",
            "bytes_received": 0, "bytes_total": 0,
            "filename": str(detail), "updated": ts(),
        }
    elif event == "firmware_downloaded":
        if serial in progress_state:
            progress_state[serial].update({
                "step": "firmware_install", "pct": 100,
                "msg": f"Firmware downloaded — starting install",
                "updated": ts(),
            })
    elif event == "firmware_applied":
        if serial in progress_state:
            progress_state[serial].update({
                "step": "firmware_install_done", "pct": 100,
                "msg": f"Firmware installed: {detail}",
                "updated": ts(),
            })
    elif event == "firmware_warning":
        if serial in progress_state:
            progress_state[serial].update({
                "msg": f"WARNING: {detail}",
                "updated": ts(),
            })
    elif event == "firmware_failed" or event == "config_failed":
        if serial in progress_state:
            progress_state[serial].update({
                "step": "failed", "msg": f"FAILED: {event} — {detail}",
                "updated": ts(),
            })

    # Mark serial as complete — unblocks higher-priority switches waiting in manifest
    if event == "ztp_complete":
        completed_serials[serial] = True
        priority = inventory.get_priority(serial)
        progress_state[serial] = {
            "step": "done", "pct": 100,
            "msg": f"ZTP complete: {detail}",
            "bytes_received": 0, "bytes_total": 0,
            "filename": "", "updated": ts(),
        }
        logger.info(f"[COMPLETE] {serial} (priority {priority}) reported ztp_complete — unblocking next priority")

    log_fn = logger.info if event in (
        "ztp_complete", "config_applied", "firmware_applied",
        "firmware_downloaded", "ztp_started"
    ) else logger.warning
    log_fn(f"[NOTIFY] {serial} → {event}: {detail}")

    # Append to per-device log file
    device_log = os.path.join(LOG_DIR, f"{serial}.log")
    with open(device_log, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return jsonify({"status": "ok"}), 200


# -----------------------------------------------------------
# API: Priority status
# -----------------------------------------------------------

@app.route("/api/progress", methods=["GET"])
def api_progress_all():
    """Return live progress for all switches."""
    result = []
    for sw in inventory.list_switches():
        serial   = str(sw.get("serial", "")).upper()
        platform = sw.get("platform", "eos")
        vendor   = "cisco" if platform == "cisco_ios" else "arista"
        prog     = progress_state.get(serial, {
            "step": "not_started", "pct": 0,
            "msg": "Waiting for switch to boot",
            "bytes_received": 0, "bytes_total": 0,
            "updated": None,
        })
        result.append({
            "serial":      serial,
            "description": sw.get("description", ""),
            "vendor":      vendor,
            "priority":    int(sw.get("priority", 99)),
            "completed":   completed_serials.get(serial, False),
            **prog,
        })
    return jsonify(sorted(result, key=lambda x: x["priority"]))


@app.route("/api/progress/<serial>", methods=["GET"])
def api_progress_serial(serial):
    """Return live progress for a specific switch."""
    serial = serial.upper()
    prog = progress_state.get(serial, {
        "step": "not_started", "pct": 0,
        "msg": "Waiting for switch to boot",
        "bytes_received": 0, "bytes_total": 0,
        "updated": None,
    })
    return jsonify({"serial": serial, **prog})


@app.route("/api/priority", methods=["GET"])
def api_priority_status():
    """Show provisioning priority status — which switches are done and which are waiting."""
    result = []
    for sw in inventory.list_switches():
        serial   = str(sw.get("serial", "")).upper()
        priority = int(sw.get("priority", 99))
        done     = completed_serials.get(serial, False)
        clear, reason = is_priority_clear(serial)
        result.append({
            "serial":      serial,
            "description": sw.get("description", ""),
            "priority":    priority,
            "completed":   done,
            "clear_to_go": clear,
            "waiting_for": reason if not clear else None,
        })
    return jsonify(sorted(result, key=lambda x: x["priority"]))


@app.route("/api/status", methods=["GET"])
def api_status():
    """
    Full provisioning status overview.
    Shows all switches grouped by priority with vendor, completion state,
    and what each switch is waiting for.
    """
    groups = {}
    for sw in inventory.list_switches():
        serial   = str(sw.get("serial", "")).upper()
        priority = int(sw.get("priority", 99))
        platform = sw.get("platform", "eos")
        vendor   = "cisco" if platform == "cisco_ios" else "arista"
        done     = completed_serials.get(serial, False)
        clear, reason = is_priority_clear(serial)

        if priority not in groups:
            groups[priority] = []
        groups[priority].append({
            "serial":      serial,
            "description": sw.get("description", ""),
            "vendor":      vendor,
            "platform":    platform,
            "firmware":    sw.get("firmware", ""),
            "config":      sw.get("config", ""),
            "priority":    priority,
            "completed":   done,
            "clear_to_go": clear,
            "waiting_for": reason if not clear else None,
        })

    return jsonify({
        "timestamp":        ts(),
        "completed_count":  len(completed_serials),
        "total_switches":   len(inventory.list_switches()),
        "completed_serials": list(completed_serials.keys()),
        "provisioning_order": [
            {
                "priority": p,
                "label": f"Priority {p} — {'parallel' if len(groups[p]) > 1 else 'single'}",
                "switches": groups[p],
                "all_complete": all(s["completed"] for s in groups[p]),
            }
            for p in sorted(groups.keys())
        ]
    })


@app.route("/api/priority/reset", methods=["POST"])
def api_priority_reset():
    """Reset completion state — useful for re-provisioning."""
    data   = request.get_json(silent=True) or {}
    serial = data.get("serial", "").upper()
    if serial:
        completed_serials.pop(serial, None)
        logger.info(f"Priority reset for {serial}")
        return jsonify({"status": "reset", "serial": serial})
    else:
        completed_serials.clear()
        logger.info("Priority state fully reset")
        return jsonify({"status": "reset_all"})


# -----------------------------------------------------------
# API: Management (list, add, remove switches)
# -----------------------------------------------------------

@app.route("/api/switches", methods=["GET"])
def api_list_switches():
    """List all switches in inventory, sorted by priority."""
    return jsonify(inventory.list_switches())


@app.route("/api/switches/<serial>", methods=["GET"])
def api_get_switch(serial):
    return jsonify(inventory.get_manifest(serial.upper()))


@app.route("/api/switches", methods=["POST"])
def api_add_switch():
    data = request.get_json(silent=True)
    if not data or "serial" not in data:
        return jsonify({"error": "serial is required"}), 400
    entry = inventory.add_switch(
        serial      = data["serial"],
        config      = data.get("config",      "generic.cfg"),
        firmware    = data.get("firmware",    "EOS-4.34.3M.swi"),
        description = data.get("description", ""),
        platform    = data.get("platform",    "eos"),
        tags        = data.get("tags",        []),
        priority    = int(data.get("priority", 99)),
    )
    return jsonify(entry), 201


@app.route("/api/switches/<serial>", methods=["DELETE"])
def api_remove_switch(serial):
    removed = inventory.remove_switch(serial.upper())
    if removed:
        return jsonify({"status": "removed", "serial": serial.upper()}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/api/inventory/reload", methods=["POST"])
def api_reload_inventory():
    inventory.reload()
    return jsonify({"status": "reloaded", "count": len(inventory.list_switches())}), 200


# -----------------------------------------------------------
# API: Events log
# -----------------------------------------------------------

@app.route("/api/events", methods=["GET"])
def api_events():
    return jsonify(ztp_events[-500:])


@app.route("/api/events/<serial>", methods=["GET"])
def api_events_serial(serial):
    serial = serial.upper()
    return jsonify([e for e in ztp_events if e.get("serial") == serial])


# -----------------------------------------------------------
# API: File listings
# -----------------------------------------------------------

@app.route("/api/configs", methods=["GET"])
def api_list_configs():
    config_dir = os.path.join(BASE_DIR, "configs")
    files = [f for f in os.listdir(config_dir) if os.path.isfile(os.path.join(config_dir, f))]
    return jsonify(sorted(files))


@app.route("/api/firmware", methods=["GET"])
def api_list_firmware():
    fw_dir = os.path.join(BASE_DIR, "firmware")
    files = [f for f in os.listdir(fw_dir) if os.path.isfile(os.path.join(fw_dir, f))]
    return jsonify(sorted(files))


# -----------------------------------------------------------
# Health check
# -----------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":           "ok",
        "switches":         len(inventory.list_switches()),
        "events_recorded":  len(ztp_events),
        "completed":        list(completed_serials.keys()),
        "timestamp":        ts(),
    })


# -----------------------------------------------------------
# Main
# -----------------------------------------------------------

logger.info(f"Starting Arista ZTP Server on {HOST}:{PORT}")
logger.info(f"Base directory  : {BASE_DIR}")
logger.info(f"Inventory file  : {INVENTORY_PATH}")
logger.info(f"Priority timeout: {PRIORITY_WAIT_TIMEOUT}s")

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False, threaded=True)