#!/usr/bin/env python3
# ============================================================
# ztp_cli.py - Command-line management tool for ZTP server
# Usage: python3 ztp_cli.py [command] [args]
# ============================================================

import argparse
import json
import sys
import requests

DEFAULT_SERVER = "http://localhost:8080"


def get(server, path):
    try:
        r = requests.get(f"{server}{path}", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def post(server, path, data):
    try:
        r = requests.post(f"{server}{path}", json=data, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def delete(server, path):
    try:
        r = requests.delete(f"{server}{path}", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def pp(data):
    print(json.dumps(data, indent=2))


def cmd_list(args):
    data = get(args.server, "/api/switches")
    if not data:
        print("No switches in inventory.")
        return
    print(f"{'Serial':<20} {'Description':<30} {'Config':<25} {'Firmware':<30} {'Platform'}")
    print("-" * 115)
    for sw in data:
        print(f"{sw.get('serial',''):<20} {sw.get('description',''):<30} {sw.get('config',''):<25} {sw.get('firmware',''):<30} {sw.get('platform','')}")


def cmd_get(args):
    data = get(args.server, f"/api/switches/{args.serial}")
    pp(data)


def cmd_add(args):
    payload = {
        "serial":      args.serial,
        "config":      args.config,
        "firmware":    args.firmware,
        "description": args.description or "",
        "platform":    args.platform,
        "tags":        args.tags.split(",") if args.tags else [],
    }
    data = post(args.server, "/api/switches", payload)
    print("Switch added/updated:")
    pp(data)


def cmd_remove(args):
    data = delete(args.server, f"/api/switches/{args.serial}")
    pp(data)


def cmd_events(args):
    path = f"/api/events/{args.serial}" if args.serial else "/api/events"
    data = get(args.server, path)
    if not data:
        print("No events.")
        return
    for e in data:
        print(f"[{e['timestamp']}] {e.get('serial','?'):20} {e.get('event','?'):25} {e.get('detail','')}")


def cmd_reload(args):
    data = post(args.server, "/api/inventory/reload", {})
    pp(data)


def cmd_health(args):
    data = get(args.server, "/health")
    pp(data)


def cmd_configs(args):
    data = get(args.server, "/api/configs")
    for f in data:
        print(f)


def cmd_firmware(args):
    data = get(args.server, "/api/firmware")
    for f in data:
        print(f)


# -----------------------------------------------------------
# Argument parser
# -----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Arista ZTP Server CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 ztp_cli.py list
  python3 ztp_cli.py get SN0001ABCD
  python3 ztp_cli.py add SN0004MNOP --config SN0004MNOP.cfg --firmware EOS64-4.34.3M.swi --platform eos64 --description "Spine Switch"
  python3 ztp_cli.py remove SN0004MNOP
  python3 ztp_cli.py events
  python3 ztp_cli.py events SN0001ABCD
  python3 ztp_cli.py reload
  python3 ztp_cli.py health
  python3 ztp_cli.py configs
  python3 ztp_cli.py firmware
        """
    )
    parser.add_argument("--server", default=DEFAULT_SERVER, help="ZTP server base URL")

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    sub.add_parser("list", help="List all switches in inventory")

    # get
    p_get = sub.add_parser("get", help="Get manifest for a serial number")
    p_get.add_argument("serial")

    # add
    p_add = sub.add_parser("add", help="Add or update a switch")
    p_add.add_argument("serial")
    p_add.add_argument("--config",      required=True)
    p_add.add_argument("--firmware",    required=True)
    p_add.add_argument("--platform",    default="eos", choices=["eos", "eos64"])
    p_add.add_argument("--description", default="")
    p_add.add_argument("--tags",        default="", help="Comma-separated tags")

    # remove
    p_rm = sub.add_parser("remove", help="Remove a switch from inventory")
    p_rm.add_argument("serial")

    # events
    p_ev = sub.add_parser("events", help="Show ZTP events")
    p_ev.add_argument("serial", nargs="?", default=None)

    # reload
    sub.add_parser("reload", help="Hot-reload inventory YAML")

    # health
    sub.add_parser("health", help="Show server health")

    # configs / firmware
    sub.add_parser("configs",  help="List available config files")
    sub.add_parser("firmware", help="List available firmware files")

    args = parser.parse_args()

    dispatch = {
        "list":     cmd_list,
        "get":      cmd_get,
        "add":      cmd_add,
        "remove":   cmd_remove,
        "events":   cmd_events,
        "reload":   cmd_reload,
        "health":   cmd_health,
        "configs":  cmd_configs,
        "firmware": cmd_firmware,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()