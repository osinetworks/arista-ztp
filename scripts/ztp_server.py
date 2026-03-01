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


@app.route("/bootstrap/cisco",       methods=["GET"])
@app.route("/bootstrap/cisco.cfg",   methods=["GET"])
def serve_bootstrap_cisco():
    """
    Serve a Cisco AutoInstall-compatible IOS config stub for C9200L/C9200 switches.

    C9200L does NOT support Python/Guest Shell ZTP. It uses Cisco AutoInstall:
    - DHCP option 67 → URL of a plain IOS text config
    - Switch downloads the config and applies it as startup-config on boot

    Strategy:
    1. Serve a minimal stub IOS config (no mgmt IP — avoids conflicts)
    2. Embed an EEM applet that fires on first boot, discovers the switch's
       real serial via CLI, fetches its manifest from our ZTP API, downloads
       and applies the real config, handles firmware, then notifies ZTP complete.

    The EEM applet uses Tcl (natively available on all IOS XE versions) to
    perform HTTP requests and CLI configuration — no Guest Shell required.
    """
    client_ip = request.remote_addr
    logger.info(f"[CISCO] AutoInstall config requested by {client_ip}")

    # C9200L runs CAT9K_LITE_IOSXE with ~1.9 GB flash.
    # Guest Shell needs 1.1 GB free to activate — repeated failed ZTP attempts
    # leave guestshell.tar on flash, exhausting space → "Script execution failed".
    #
    # Solution: serve a plain IOS config stub (text/plain) via a URL WITHOUT .py extension.
    # AutoInstall applies it as startup-config on first boot.
    # An embedded EEM applet then handles the rest via native IOS XE Tcl (no Guest Shell needed).
    #
    # Key: DHCP Option 67 must point to /bootstrap/cisco (no .py) so the switch
    # treats the HTTP response as a config file, not a Python script execution trigger.
    config = _build_cisco_autoinstall_config(SERVER_IP, str(PORT))
    return config, 200, {"Content-Type": "text/plain"}


def _build_cisco_autoinstall_config(server_ip: str, server_port: str) -> str:
    """
    Build a minimal IOS XE config stub that bootstraps ZTP via EEM + Tcl.

    The stub config is applied by AutoInstall as startup-config on first boot.
    It contains an EEM applet that fires 60 seconds after boot, reads the
    switch serial number, downloads a per-serial Tcl script, and executes it.

    Key design decisions for C9200L reliability:
    - Put 'version 17.12' at the top so AutoInstall successfully parses it.
    - Put 'hostname Switch' to satisfy AutoInstall defaults.
    - Use a countdown timer (90s) to give interfaces time to come up securely.
    - Use 'cli command "show version"' to get serial, because 'show inventory'
      output format varies by model. 'show version' always has 'Processor board ID'.
    - Use 'string match' instead of 'regexp' for serial extraction (more portable
      across EEM versions on IOS XE 16.x / 17.x).
    - Download per-serial Tcl using 'copy http: flash:' with 'noprompt'.
    - All EEM actions are numbered with leading zeros to avoid ordering bugs.
    - 'authorization bypass' allows EEM to run before AAA is configured.
    """
    # Build the config without f-string issues by careful string construction.
    # The $serial EEM variable must NOT be f-string interpolated.
    lines = [
        "version 17.12",
        "service timestamps debug datetime msec",
        "service timestamps log datetime msec",
        "!",
        "hostname Switch",
        "!",
        "! ============================================================",
        "! Cisco AutoInstall Stub Config - Generated by ZTP Server",
        "! Applied automatically by IOS XE AutoInstall on first boot.",
        "! ============================================================",
        "!",
        "! Disable AutoInstall after this first successful apply.",
        "! The EEM applet below will handle the rest of ZTP.",
        "no service config",
        "!",
        "! EEM ZTP Bootstrap applet.",
        "! Fires 90s after boot — gives interfaces time to come up securely.",
        "! Reads serial from 'show version', downloads per-serial Tcl script,",
        "! then runs it. All heavy lifting is done inside the Tcl script.",
        "event manager applet ZTP-Bootstrap authorization bypass",
        " event timer countdown time 90",
        " action 001 syslog msg \"[ZTP] Bootstrap starting — reading serial\"",
        " action 002 cli command \"enable\"",
        " action 003 cli command \"show version\"",
        " action 004 regexp \"Processor board ID ([A-Z0-9]+)\" \"$_cli_result\" match serial",
        " action 005 if $_regexp_result ne 1",
        " action 006   syslog msg \"[ZTP] WARNING: show version serial failed — trying show inventory\"",
        " action 007   cli command \"show inventory\"",
        " action 008   regexp \"SN: ([A-Z0-9]+)\" \"$_cli_result\" match serial",
        " action 009 end",
        " action 010 if $_regexp_result eq 1",
        " action 011   syslog msg \"[ZTP] Serial detected — downloading Tcl script\"",
        # Download Tcl script — noprompt suppresses all confirmation prompts
        f" action 012   cli command \"copy http://{server_ip}:{server_port}/api/eem/" + r"$serial" + " flash:ztp.tcl noprompt\"",
        " action 013   syslog msg \"[ZTP] Running Tcl script...\"",
        " action 014   cli command \"tclsh flash:ztp.tcl\"",
        " action 015   syslog msg \"[ZTP] Tcl script completed\"",
        " action 016 else",
        " action 017   syslog msg \"[ZTP] ERROR: Could not detect serial number — aborting ZTP\"",
        " action 018 end",
        "!",
        "end",
        "",
    ]
    return "\n".join(lines)


@app.route("/api/eem/<serial>", methods=["GET"])
def serve_eem_script(serial):
    """
    Serve a per-serial Tcl ZTP script for Cisco IOS XE.

    Called by the EEM applet embedded in the AutoInstall stub config.
    The Tcl script runs in the IOS XE 'tclsh' environment (always available).

    Tcl in IOS XE can:
    - Execute CLI commands via 'ios_config' and 'typeahead'
    - Use the 'http' package to download files
    - Read/write flash filesystem

    The script:
    1. Contacts our API to get the switch's config and firmware filenames
    2. Downloads config via 'copy http: running-config'
    3. Saves running-config to startup-config
    4. Optionally downloads and installs firmware
    5. Notifies ZTP server (ztp_complete)
    6. Reloads the switch
    """
    serial = serial.upper().strip()
    client_ip = request.remote_addr
    logger.info(f"[CISCO-EEM] Tcl script requested for serial={serial} by {client_ip}")

    # Fetch manifest for this serial
    manifest   = inventory.get_manifest(serial)
    config_file   = manifest.get("config",   "generic.cfg")
    firmware_file = manifest.get("firmware", "")
    description   = manifest.get("description", "Unknown")

    base_url = f"http://{SERVER_IP}:{PORT}"

    # Log the event
    ztp_events.append({
        "timestamp": ts(),
        "serial":    serial,
        "event":     "eem_script_served",
        "detail":    {"config": config_file, "firmware": firmware_file},
        "client_ip": client_ip,
    })
    progress_state[serial] = {
        "step": "starting", "pct": 5,
        "msg": f"EEM Tcl script fetched — applying config: {config_file}",
        "bytes_received": 0, "bytes_total": 0,
        "filename": config_file, "updated": ts(),
    }

    # Generate the per-serial Tcl script
    # Tcl in IOS XE uses 'ios_config' proc and CLI exec via 'exec' calls
    tcl = _build_cisco_tcl_script(serial, base_url, config_file, firmware_file, description)

    logger.info(f"[CISCO-EEM] Serving Tcl script for {serial}: config={config_file} firmware={firmware_file}")
    return tcl, 200, {"Content-Type": "text/plain"}


def _build_cisco_tcl_script(
    serial: str,
    base_url: str,
    config_file: str,
    firmware_file: str,
    description: str,
) -> str:
    """
    Build a Tcl ZTP script for IOS XE tclsh.
    """
    fw_section = ""
    if firmware_file:
        fw_section = f"""
# -------------------------------------------------------
# STEP 2/3: Firmware upgrade
# -------------------------------------------------------
puts "\\[ZTP\\] STEP 2/3: Downloading firmware {firmware_file}..."
set fw_url "{base_url}/firmware/{firmware_file}"
set fw_flash "flash:{firmware_file}"

# Check if already on flash via full listing (reliable on all IOS XE versions)
set dir_out ""
catch {{exec "dir flash:"}} dir_out
if {{[string match "*{firmware_file}*" $dir_out]}} {{
    puts "\\[ZTP\\] Firmware already in flash — skipping download."
}} else {{
    # Remove inactive install packages FIRST to free flash space.
    # C9200L flash fills up after prior installs — REQUIRED before upgrade.
    puts "\\[ZTP\\] Removing inactive packages to free flash..."
    typeahead "y\\n"
    catch {{exec "install remove inactive"}} rm_out
    puts "\\[ZTP\\] Remove inactive: $rm_out"

    puts "\\[ZTP\\] Downloading firmware (may take 10-30 min)..."
    typeahead "\\n"
    catch {{exec "copy $fw_url $fw_flash"}} fw_result
    puts "\\[ZTP\\] Firmware download: $fw_result"
}}

# Set boot system to packages.conf (Install Mode requirement for C9200L)
puts "\\[ZTP\\] Setting boot system to packages.conf..."
ios_config "no boot system"
ios_config "boot system flash:packages.conf"
typeahead "\\n"
catch {{exec "write memory"}} wm_out
puts "\\[ZTP\\] write memory: $wm_out"

# Notify ztp_complete BEFORE install — switch loses connectivity on reload.
notify "ztp_complete" "reloading_for_firmware"

# Install + activate + commit (Cisco Install Mode — correct for C9200L/Cat9k).
# Switch reloads automatically during 'activate' — session will drop.
puts "\\[ZTP\\] Running: install add file $fw_flash activate commit"
puts "\\[ZTP\\] Switch reloads automatically — done."
typeahead "\\n\\n\\n"
catch {{
    exec "install add file $fw_flash activate commit"
}} inst_out
puts "\\[ZTP\\] Install result: $inst_out"
notify "firmware_applied" "{firmware_file}"
puts "\\[ZTP\\] STEP 2/3: OK — switch is rebooting"
# Skip explicit reload — 'install activate' already reboots the switch
return
"""

    script = f"""#!/usr/bin/tclsh
# ============================================================
# Cisco IOS XE ZTP Tcl Script
# Serial: {serial} — {description}
# Generated by ZTP Server. Runs in IOS XE tclsh (no Guest Shell).
# ============================================================
puts "===================================================="
puts "\\[ZTP\\] ZTP starting for {serial} ({description})"
puts "===================================================="

# Notify helper
# Uses http::geturl (Tcl http package — always available in IOS XE tclsh).
# Falls back to silent failure so ZTP continues even if server unreachable.
# -------------------------------------------------------
package require http

proc notify {{event detail}} {{
    set url "{base_url}/api/notify_get?serial={serial}&event=$event&detail=$detail"
    catch {{
        set token [http::geturl $url -timeout 10000]
        http::cleanup $token
    }}
}}

notify "ztp_started" "tcl_script_running"

# -------------------------------------------------------
# STEP 1/3: Apply configuration
# -------------------------------------------------------
puts "\\[ZTP\\] STEP 1/3: Applying config {config_file}..."

# Download config to flash (no interactive prompt — flash: dest is non-interactive)
puts "\\[ZTP\\] Downloading config to flash..."
typeahead "\\n"
catch {{exec "copy {base_url}/configs/{config_file} flash:ztp_cfg.txt"}} dl_result
puts "\\[ZTP\\] Config download: $dl_result"

# Copy flash file to startup-config (one \\n to confirm dest filename)
puts "\\[ZTP\\] Writing startup-config..."
typeahead "\\n"
catch {{exec "copy flash:ztp_cfg.txt startup-config"}} sc_result
puts "\\[ZTP\\] startup-config: $sc_result"

# Clean up temp file
catch {{exec "delete /force flash:ztp_cfg.txt"}}

notify "config_applied" "{config_file}"
puts "\\[ZTP\\] STEP 1/3: OK"
{fw_section}
# -------------------------------------------------------
# STEP 3/3: Notify ZTP complete and reload
# -------------------------------------------------------
puts "\\[ZTP\\] STEP 3/3: Notifying ZTP server and reloading..."
notify "ztp_complete" "success"
puts "\\[ZTP\\] ZTP complete for {serial}. Reloading in 1 minute..."
typeahead "\\n"
catch {{exec "reload in 1 reason ZTP-complete"}}
puts "\\[ZTP\\] Done."
"""
    return script




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


@app.route("/api/notify_get", methods=["GET"])
def api_notify_get():
    """
    GET-based notification endpoint for Cisco IOS XE Tcl scripts.
    IOS XE tclsh's 'exec "http get URL"' can only do GET requests,
    so this mirrors /api/notify but accepts parameters via query string.
    """
    serial = request.args.get("serial", "UNKNOWN").upper()
    event  = request.args.get("event",  "unknown")
    detail = request.args.get("detail", "")

    entry = {
        "timestamp": ts(),
        "serial":    serial,
        "event":     event,
        "detail":    detail,
        "client_ip": request.remote_addr,
    }
    ztp_events.append(entry)

    # Reuse the same state-update logic as the POST endpoint
    if event == "ztp_complete":
        completed_serials[serial] = True
        priority = inventory.get_priority(serial)
        progress_state[serial] = {
            "step": "done", "pct": 100,
            "msg": f"ZTP complete (via Tcl): {detail}",
            "bytes_received": 0, "bytes_total": 0,
            "filename": "", "updated": ts(),
        }
        logger.info(f"[COMPLETE/GET] {serial} (priority {priority}) reported ztp_complete")
    elif event == "ztp_started":
        progress_state[serial] = {
            "step": "starting", "pct": 5,
            "msg": "ZTP Tcl script started",
            "bytes_received": 0, "bytes_total": 0,
            "filename": "", "updated": ts(),
        }
    elif event in ("config_applied", "firmware_applied", "firmware_downloaded"):
        pct = {"config_applied": 33, "firmware_downloaded": 66, "firmware_applied": 95}.get(event, 50)
        progress_state[serial] = {
            "step": event, "pct": pct,
            "msg": f"{event}: {detail}",
            "bytes_received": 0, "bytes_total": 0,
            "filename": str(detail), "updated": ts(),
        }
    elif event in ("config_failed", "firmware_failed"):
        if serial in progress_state:
            progress_state[serial].update({
                "step": "failed",
                "msg": f"FAILED ({event}): {detail}",
                "updated": ts(),
            })

    log_fn = logger.info if event in (
        "ztp_complete", "config_applied", "firmware_applied",
        "firmware_downloaded", "ztp_started"
    ) else logger.warning
    log_fn(f"[NOTIFY/GET] {serial} → {event}: {detail}")

    # Append to per-device log file
    device_log = os.path.join(LOG_DIR, f"{serial}.log")
    with open(device_log, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return "ok", 200


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
            "clear":       clear,
            "reason":      reason,
        })

    # Sort groups
    result = []
    for p in sorted(groups.keys()):
        result.append({
            "priority": p,
            "switches": sorted(groups[p], key=lambda x: x["serial"])
        })
    return jsonify(result)


@app.route("/api/switches", methods=["GET"])
def api_switches():
    """Returns the parsed inventory (list of all configured switches)."""
    return jsonify(inventory.list_switches())


@app.route("/api/events", methods=["GET"])
def api_events():
    """Return the recent ZTP event log."""
    # Return last 100 events
    return jsonify(ztp_events[-100:])


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "inventory_loaded": inventory is not None})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
