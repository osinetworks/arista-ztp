#!/usr/bin/env python3
# ============================================================
# inventory_manager.py
# Loads and queries the ZTP inventory YAML file.
# Supports priority-based provisioning order.
# ============================================================

import yaml
import os
import logging
from typing import Optional, Dict, Any, List

INVENTORY_PATH = os.environ.get("INVENTORY_PATH", "/var/www/ztp/config/inventory.yaml")

logger = logging.getLogger("ztp.inventory")


class InventoryManager:
    def __init__(self, path: str = INVENTORY_PATH):
        self.path = path
        self._data: Dict = {}
        self._index: Dict[str, Dict] = {}
        self.reload()

    def reload(self):
        """Load / reload the inventory YAML file."""
        try:
            with open(self.path, "r") as f:
                self._data = yaml.safe_load(f) or {}
            self._build_index()
            logger.info(f"Inventory loaded: {len(self._index)} switches from {self.path}")
        except FileNotFoundError:
            logger.error(f"Inventory file not found: {self.path}")
            self._data = {}
            self._index = {}
        except yaml.YAMLError as e:
            logger.error(f"Inventory YAML parse error: {e}")

    def _build_index(self):
        """Build serial → switch dict for O(1) lookup."""
        self._index = {}
        for switch in self._data.get("switches", []):
            serial = str(switch.get("serial", "")).upper().strip()
            if serial:
                self._index[serial] = switch

    def get_defaults(self) -> Dict[str, Any]:
        return self._data.get("defaults", {
            "firmware": "EOS-4.34.3M.swi",
            "config":   "generic.cfg",
            "platform": "eos",
            "priority": 99,
        })

    def get_priority(self, serial: str) -> int:
        """Return the provisioning priority for a serial number."""
        serial = serial.upper().strip()
        if serial in self._index:
            return int(self._index[serial].get("priority", 99))
        return int(self.get_defaults().get("priority", 99))

    def get_serials_with_priority(self, priority: int) -> List[str]:
        """Return all serial numbers that have a given priority."""
        return [
            str(sw.get("serial", "")).upper().strip()
            for sw in self._data.get("switches", [])
            if int(sw.get("priority", 99)) == priority
        ]

    def get_all_priorities(self) -> List[int]:
        """Return sorted list of all unique priority values in inventory."""
        priorities = set()
        for sw in self._data.get("switches", []):
            priorities.add(int(sw.get("priority", 99)))
        return sorted(priorities)

    def get_manifest(self, serial: str) -> Dict[str, Any]:
        """
        Return the manifest (config, firmware, description, priority) for a serial.
        Falls back to defaults if serial is not in inventory.
        """
        serial = serial.upper().strip()
        defaults = self.get_defaults()

        if serial in self._index:
            switch = self._index[serial]
            logger.info(f"Manifest hit  : {serial} → {switch.get('config')} / {switch.get('firmware')} (priority {switch.get('priority', 99)})")
            return {
                "serial":      serial,
                "description": switch.get("description", ""),
                "platform":    switch.get("platform",    defaults.get("platform", "eos")),
                "firmware":    switch.get("firmware",    defaults.get("firmware")),
                "config":      switch.get("config",      defaults.get("config")),
                "priority":    int(switch.get("priority", 99)),
                "tags":        switch.get("tags",        []),
                "source":      "inventory",
            }
        else:
            logger.warning(f"Manifest miss : {serial} → using defaults")
            return {
                "serial":      serial,
                "description": "Unknown / Unregistered Switch",
                "platform":    defaults.get("platform", "eos"),
                "firmware":    defaults.get("firmware", "EOS-4.34.3M.swi"),
                "config":      defaults.get("config",   "generic.cfg"),
                "priority":    int(defaults.get("priority", 99)),
                "tags":        [],
                "source":      "default",
            }

    def list_switches(self) -> List[Dict]:
        """Return all switches sorted by priority."""
        switches = list(self._index.values())
        return sorted(switches, key=lambda s: int(s.get("priority", 99)))

    def add_switch(self, serial: str, config: str, firmware: str,
                   description: str = "", platform: str = "eos",
                   tags=None, priority: int = 99):
        """Add or update a switch entry and persist to YAML."""
        serial = serial.upper().strip()
        entry = {
            "serial":      serial,
            "description": description,
            "platform":    platform,
            "firmware":    firmware,
            "config":      config,
            "priority":    priority,
            "tags":        tags or [],
        }
        self._index[serial] = entry

        switches = self._data.setdefault("switches", [])
        for i, sw in enumerate(switches):
            if str(sw.get("serial", "")).upper() == serial:
                switches[i] = entry
                break
        else:
            switches.append(entry)

        self._persist()
        logger.info(f"Switch added/updated: {serial} (priority {priority})")
        return entry

    def remove_switch(self, serial: str) -> bool:
        serial = serial.upper().strip()
        if serial not in self._index:
            return False
        del self._index[serial]
        self._data["switches"] = [
            sw for sw in self._data.get("switches", [])
            if str(sw.get("serial", "")).upper() != serial
        ]
        self._persist()
        logger.info(f"Switch removed: {serial}")
        return True

    def _persist(self):
        """Write current state back to YAML file."""
        try:
            with open(self.path, "w") as f:
                yaml.dump(self._data, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            logger.error(f"Failed to persist inventory: {e}")
