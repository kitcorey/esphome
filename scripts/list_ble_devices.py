"""List all BLE devices and entities actively used in Home Assistant.

Reads HA's .storage files directly to find:
- Native BLE devices (Govee, Hunter Douglas, etc.) from the device registry
- ESPHome-bridged BLE sensors (Xiaomi temp sensors, RSSI) from the entity registry

Excludes discovered-but-ignored entries and non-functional config entries.

Usage:
    uv run python list_ble_devices.py
    uv run python list_ble_devices.py --format json
    uv run python list_ble_devices.py --config-dir /path/to/ha/config
"""

import argparse
import json
import re
import sys
from pathlib import Path


DEFAULT_CONFIG_DIR = "/home/kit/docker/homeassistant/config"

# Patterns that identify ESPHome BLE sensor entities
ESPHOME_BLE_PATTERNS = [
    r"lywsd",      # Xiaomi LYWSD02/LYWSD03 sensors
    r"ble_rssi",   # BLE RSSI sensors
    r"xiaomi",     # Other Xiaomi sensors
    r"bthome",     # BTHome devices
    r"pvvx",       # PVVX firmware sensors
    r"atc",        # ATC firmware sensors
]


def load_storage_file(config_dir: Path, filename: str) -> dict:
    """Load a JSON file from HA's .storage directory."""
    path = config_dir / ".storage" / filename
    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def get_ignored_entry_ids(config_entries: dict) -> set:
    """Get config entry IDs that are ignored (discovered but not set up)."""
    ignored = set()
    for entry in config_entries.get("data", {}).get("entries", []):
        if entry.get("source") == "ignore":
            ignored.add(entry.get("entry_id"))
    return ignored


def get_native_ble_devices(device_registry: dict, ignored_entries: set) -> list:
    """Get devices with bluetooth identifiers that aren't ignored."""
    devices = []
    for device in device_registry.get("data", {}).get("devices", []):
        # Check for bluetooth identifier
        mac = None
        for identifier in device.get("identifiers", []):
            if len(identifier) >= 2 and identifier[0] == "bluetooth":
                mac = identifier[1]
                break

        if not mac:
            continue

        # Check if any config entry is ignored
        config_entries = device.get("config_entries", [])
        if all(ce in ignored_entries for ce in config_entries):
            continue

        # Use user-assigned name if available
        name = device.get("name_by_user") or device.get("name") or "Unknown"

        devices.append({
            "name": name,
            "manufacturer": device.get("manufacturer") or "Unknown",
            "model": device.get("model") or "Unknown",
            "mac": mac,
            "device_id": device.get("id"),
        })

    return sorted(devices, key=lambda d: d["name"].lower())


def get_esphome_ble_entities(entity_registry: dict, device_registry: dict) -> list:
    """Get ESPHome entities that are BLE sensors."""
    pattern = re.compile("|".join(ESPHOME_BLE_PATTERNS), re.IGNORECASE)
    entities = []

    # Build device_id -> device_name lookup
    device_names = {}
    for device in device_registry.get("data", {}).get("devices", []):
        device_id = device.get("id")
        device_name = device.get("name_by_user") or device.get("name") or "unknown"
        device_names[device_id] = device_name

    for entity in entity_registry.get("data", {}).get("entities", []):
        if entity.get("platform") != "esphome":
            continue

        entity_id = entity.get("entity_id", "")
        if not pattern.search(entity_id):
            continue

        # Skip disabled entities
        if entity.get("disabled_by"):
            continue

        # Look up proxy device name from device registry
        device_id = entity.get("device_id")
        proxy = device_names.get(device_id, "unknown")

        name = entity.get("original_name") or entity.get("name") or entity_id

        entities.append({
            "entity_id": entity_id,
            "name": name,
            "proxy_device": proxy,
            "unique_id": entity.get("unique_id"),
        })

    return sorted(entities, key=lambda e: e["entity_id"])


def format_table(devices: list, entities: list) -> str:
    """Format output as a human-readable table."""
    lines = []

    # Native BLE devices
    lines.append(f"Native BLE Devices ({len(devices)})")
    lines.append("=" * 90)

    if devices:
        # Calculate column widths
        name_w = max(len(d["name"]) for d in devices)
        mfr_w = max(len(d["manufacturer"]) for d in devices)
        model_w = max(len(d["model"]) for d in devices)

        name_w = max(name_w, 4)  # "Name"
        mfr_w = max(mfr_w, 12)   # "Manufacturer"
        model_w = max(model_w, 5)  # "Model"

        header = f"{'Name':<{name_w}}  {'Manufacturer':<{mfr_w}}  {'Model':<{model_w}}  MAC"
        lines.append(header)
        lines.append("-" * len(header) + "-" * 17)

        for d in devices:
            lines.append(f"{d['name']:<{name_w}}  {d['manufacturer']:<{mfr_w}}  {d['model']:<{model_w}}  {d['mac']}")
    else:
        lines.append("(none)")

    lines.append("")

    # ESPHome BLE entities
    lines.append(f"ESPHome-Bridged BLE Sensors ({len(entities)})")
    lines.append("=" * 90)

    if entities:
        eid_w = max(len(e["entity_id"]) for e in entities)
        name_w = max(len(e["name"]) for e in entities)

        eid_w = max(eid_w, 9)   # "Entity ID"
        name_w = max(name_w, 4)  # "Name"

        header = f"{'Entity ID':<{eid_w}}  {'Name':<{name_w}}  Proxy Device"
        lines.append(header)
        lines.append("-" * len(header) + "-" * 5)

        for e in entities:
            lines.append(f"{e['entity_id']:<{eid_w}}  {e['name']:<{name_w}}  {e['proxy_device']}")
    else:
        lines.append("(none)")

    return "\n".join(lines)


def format_json(devices: list, entities: list) -> str:
    """Format output as JSON."""
    return json.dumps({
        "native_ble_devices": devices,
        "esphome_ble_entities": entities,
    }, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="List all BLE devices and entities actively used in Home Assistant"
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path(DEFAULT_CONFIG_DIR),
        help=f"HA config directory (default: {DEFAULT_CONFIG_DIR})",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    args = parser.parse_args()

    if not args.config_dir.exists():
        print(f"Error: Config directory not found: {args.config_dir}", file=sys.stderr)
        sys.exit(1)

    # Load storage files
    device_registry = load_storage_file(args.config_dir, "core.device_registry")
    entity_registry = load_storage_file(args.config_dir, "core.entity_registry")
    config_entries = load_storage_file(args.config_dir, "core.config_entries")

    # Get ignored config entries
    ignored_entries = get_ignored_entry_ids(config_entries)

    # Get BLE devices and entities
    devices = get_native_ble_devices(device_registry, ignored_entries)
    entities = get_esphome_ble_entities(entity_registry, device_registry)

    # Output
    if args.format == "json":
        print(format_json(devices, entities))
    else:
        print(format_table(devices, entities))


if __name__ == "__main__":
    main()
