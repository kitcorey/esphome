"""Add BLE RSSI sensors to an ESPHome YAML config.

Reads the list of BLE devices from list_ble_devices.py and appends ble_rssi
sensor entries for devices not already present in the target YAML.

Usage:
    uv run python scripts/add_rssi_sensors.py ble_tracker.yml
    uv run python scripts/add_rssi_sensors.py ble_tracker_olimex.yml --prefix "Olimex "
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def get_ble_devices() -> dict | None:
    """Get BLE device list from list_ble_devices.py."""
    script_dir = Path(__file__).parent
    result = subprocess.run(
        ["uv", "run", "python", "list_ble_devices.py", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=script_dir,
    )
    if result.returncode != 0:
        print(f"Error running list_ble_devices.py: {result.stderr}", file=sys.stderr)
        return None
    return json.loads(result.stdout)


def find_existing_rssi_macs(yaml_content: str) -> set[str]:
    """Find MAC addresses already configured as ble_rssi sensors."""
    macs = set()
    # Match mac_address lines in ble_rssi sensor blocks
    # Handles both quoted and unquoted MACs, with or without !secret
    for match in re.finditer(r'mac_address:\s*["\']?([0-9A-Fa-f:]{17})["\']?', yaml_content):
        macs.add(match.group(1).upper())
    return macs


def generate_rssi_sensors(devices: list[dict], existing_macs: set[str], prefix: str) -> str:
    """Generate YAML for ble_rssi sensors for devices not already present."""
    lines = []
    for device in devices:
        mac = device["mac"].upper()
        if mac in existing_macs:
            continue
        # Clean up device name: remove " BLE" suffix if present
        name = device["name"]
        if name.endswith(" BLE"):
            name = name[:-4]
        lines.append("  - platform: ble_rssi")
        lines.append(f'    mac_address: "{mac}"')
        lines.append(f'    name: "{prefix}{name} RSSI"')
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Add BLE RSSI sensors to an ESPHome YAML config")
    parser.add_argument("yaml_file", type=Path, help="ESPHome YAML config file to modify")
    parser.add_argument(
        "--prefix",
        default="",
        help='Name prefix for sensors (e.g., "Olimex " for the Olimex board)',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be added without modifying the file",
    )
    args = parser.parse_args()

    if not args.yaml_file.exists():
        print(f"Error: {args.yaml_file} not found", file=sys.stderr)
        sys.exit(1)

    # Get BLE devices
    ble_data = get_ble_devices()
    if not ble_data:
        sys.exit(1)

    native_devices = ble_data.get("native_ble_devices", [])
    if not native_devices:
        print("No native BLE devices found")
        sys.exit(0)

    # Read existing YAML
    yaml_content = args.yaml_file.read_text()
    existing_macs = find_existing_rssi_macs(yaml_content)

    # Generate new sensors
    new_sensors = generate_rssi_sensors(native_devices, existing_macs, args.prefix)
    if not new_sensors:
        print("All devices already have RSSI sensors configured")
        sys.exit(0)

    # Count how many will be added
    new_count = new_sensors.count("platform: ble_rssi")
    print(f"Adding {new_count} RSSI sensor(s) to {args.yaml_file}")

    if args.dry_run:
        print("\nWould append:")
        print(new_sensors)
        sys.exit(0)

    # Append to file
    with open(args.yaml_file, "a") as f:
        f.write("\n")
        f.write(new_sensors)
        f.write("\n")

    print("Done")


if __name__ == "__main__":
    main()
