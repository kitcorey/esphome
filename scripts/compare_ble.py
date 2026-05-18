"""Compare BLE RSSI performance between wESP32 and Olimex BLE tracker boards.

Queries Home Assistant's REST API for RSSI sensor history and prints a
side-by-side comparison table.

Usage:
    uv run python scripts/compare_ble.py             # Last 24 hours, compact view
    uv run python scripts/compare_ble.py --hours 1   # Last hour
    uv run python scripts/compare_ble.py --verbose   # Detailed per-device stats
"""

import argparse
import json
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

# (entity_prefix, sensor_name_prefix, column_label, short_label)
# sensor_name_prefix is what ESPHome prepends to the sensor object_id, which
# lives inside the entity_id *after* the device-name prefix. For wESP32
# sensors are bare (e.g. "LYWSD03MMC RSSI"); Olimex sensors are named
# "Olimex LYWSD03MMC RSSI" so the slug carries an extra "olimex_".
BOARDS = [
    ("ble_tracker", "", "wESP32 (chip)", "wESP32"),
    ("ble_tracker_olimex", "olimex_", "Olimex (external)", "Olimex"),
]

# ESP32 BLE radio sensitivity floor is around -97 to -100 dBm; readings
# below this are decode artifacts, not real signal. RSSI is always negative.
RSSI_MIN = -100
RSSI_MAX = 0


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
        return None
    return json.loads(result.stdout)


def validate_coverage(entities: dict, ble_devices: dict | None) -> None:
    """Report which BLE devices are missing RSSI sensors."""
    if not ble_devices:
        return

    tracked_devices = set(entities.keys())

    print("\nRSSI Coverage:")
    print("-" * 60)

    native_devices = ble_devices.get("native_ble_devices", [])
    missing = []
    for device in native_devices:
        # Normalize name to match entity discovery format
        name = device["name"]
        if name.endswith(" BLE"):
            name = name[:-4]
        slug = name.upper().replace(" ", "_")

        if slug not in tracked_devices:
            missing.append((device["name"], device["mac"]))

    if missing:
        print(f"Missing RSSI sensors for {len(missing)} device(s):")
        for name, mac in missing:
            print(f"  - {name} ({mac})")
        print("\nRun add_rssi_sensors.py to add them to your ESPHome configs.")
    else:
        print(f"All {len(native_devices)} native BLE devices have RSSI sensors")


def load_secrets():
    secrets_path = Path(__file__).parent.parent / "secrets.yaml"
    with open(secrets_path) as f:
        secrets = yaml.safe_load(f)
    url = secrets.get("ha_url")
    token = secrets.get("ha_token")
    if not url or not token or token == "YOUR_LONG_LIVED_ACCESS_TOKEN":
        print("Error: Set ha_url and ha_token in secrets.yaml")
        print("Generate a token in HA: Profile > Security > Long-Lived Access Tokens")
        sys.exit(1)
    return url.rstrip("/"), token


def get_rssi_entities(url, headers):
    """Auto-discover RSSI sensor entities from BLE tracker devices."""
    resp = requests.get(f"{url}/api/states", headers=headers, timeout=10)
    resp.raise_for_status()
    entities = {}
    # Check longer (more specific) entity prefixes first so ble_tracker_olimex
    # wins over ble_tracker for sensor.ble_tracker_olimex_* entities.
    ordered = sorted(BOARDS, key=lambda b: len(b[0]), reverse=True)
    for state in resp.json():
        eid = state["entity_id"]
        if "rssi" not in eid or not eid.startswith("sensor."):
            continue
        for entity_prefix, sensor_prefix, board_label, _ in ordered:
            full = f"sensor.{entity_prefix}_{sensor_prefix}"
            if eid.startswith(full):
                device = eid[len(full) :].replace("_rssi", "").upper()
                entities.setdefault(device, {})[board_label] = eid
                break
    return entities


def get_history(url, headers, entity_id, hours):
    """Fetch sensor history for the given time window."""
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    params = {"filter_entity_id": entity_id, "minimal_response": "", "no_attributes": ""}
    resp = requests.get(f"{url}/api/history/period/{start}", headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data or not data[0]:
        return [], 0
    values, dropped = [], 0
    for entry in data[0]:
        try:
            v = float(entry["state"])
        except (ValueError, KeyError):
            continue
        if RSSI_MIN <= v <= RSSI_MAX:
            values.append(v)
        else:
            dropped += 1
    return values, dropped


def fetch_all_history(entities, url, headers, hours):
    """Fetch history for all entities and return as dict."""
    board_labels = [b[2] for b in BOARDS]
    all_data = {}
    for device in entities:
        all_data[device] = {}
        for label in board_labels:
            eid = entities[device].get(label)
            if eid:
                all_data[device][label] = get_history(url, headers, eid, hours)
            else:
                all_data[device][label] = ([], 0)
    return all_data


def format_device_name(slug: str) -> str:
    """Convert SCREAMING_SNAKE to Title Case."""
    return slug.replace("_", " ").title()


def print_compact(entities, all_data, hours):
    """Print a compact one-line-per-device summary."""
    board_labels = [b[2] for b in BOARDS]
    short_labels = [b[3] for b in BOARDS]

    print(f"\nBLE RSSI Comparison (last {hours}h)")
    print("=" * 88)
    print(f"{'Device':<22} {short_labels[0]:>8} {short_labels[1]:>8} {'Diff':>6} {'Readings':>10}  Winner")
    print("-" * 88)

    no_data_devices = []
    wins = {short_labels[0]: 0, short_labels[1]: 0, "tie": 0}
    total_diff = 0.0
    diff_count = 0

    for device in sorted(entities):
        data = all_data[device]
        values_a = data.get(board_labels[0], ([], 0))[0]
        values_b = data.get(board_labels[1], ([], 0))[0]

        if not values_a and not values_b:
            no_data_devices.append(device)
            continue

        mean_a = statistics.mean(values_a) if values_a else None
        mean_b = statistics.mean(values_b) if values_b else None

        col_a = f"{mean_a:.1f}" if mean_a else "--"
        col_b = f"{mean_b:.1f}" if mean_b else "--"
        readings = f"{len(values_a)}/{len(values_b)}"

        if mean_a and mean_b:
            diff = mean_b - mean_a  # Positive = second board stronger
            total_diff += diff
            diff_count += 1
            if abs(diff) < 1.0:
                winner = "~tie"
                wins["tie"] += 1
                diff_str = f"{diff:+.1f}"
            elif diff > 0:
                winner = short_labels[1]
                wins[short_labels[1]] += 1
                diff_str = f"{diff:+.1f}"
            else:
                winner = short_labels[0]
                wins[short_labels[0]] += 1
                diff_str = f"{diff:+.1f}"
        elif mean_a:
            winner = f"{short_labels[0]} only"
            diff_str = "--"
        else:
            winner = f"{short_labels[1]} only"
            diff_str = "--"

        name = format_device_name(device)
        if len(name) > 21:
            name = name[:19] + ".."
        print(f"{name:<22} {col_a:>8} {col_b:>8} {diff_str:>6} {readings:>10}  {winner}")

    # Summary
    print("-" * 88)
    avg_diff = total_diff / diff_count if diff_count else 0
    if avg_diff > 1.0:
        overall = f"{short_labels[1]} by {avg_diff:+.1f} dB avg"
    elif avg_diff < -1.0:
        overall = f"{short_labels[0]} by {avg_diff:+.1f} dB avg"
    else:
        overall = "essentially tied"
    print(
        f"Summary: {short_labels[0]} wins {wins[short_labels[0]]}, "
        f"{short_labels[1]} wins {wins[short_labels[1]]}, "
        f"ties {wins['tie']} -> {overall}"
    )

    if no_data_devices:
        names = ", ".join(format_device_name(d) for d in no_data_devices[:3])
        suffix = ", ..." if len(no_data_devices) > 3 else ""
        print(f"({len(no_data_devices)} device(s) with no data: {names}{suffix})")


def print_verbose(entities, all_data, hours):
    """Print detailed per-device statistics."""
    board_labels = [b[2] for b in BOARDS]
    col_width = 22

    print(f"\nBLE RSSI Comparison - Detailed (last {hours}h)")
    print("=" * (28 + col_width * len(board_labels)))
    header = " " * 28
    for label in board_labels:
        header += f"{label:<{col_width}}"
    print(header)
    print("-" * (28 + col_width * len(board_labels)))

    for device in sorted(entities):
        print(f"\n{format_device_name(device)}")
        board_data = all_data[device]

        rows = [
            ("Mean RSSI (dBm)", lambda v, d: f"{statistics.mean(v):.1f}"),
            ("Min / Max", lambda v, d: f"{min(v):.0f} / {max(v):.0f}"),
            ("Std Dev", lambda v, d: f"{statistics.stdev(v):.1f}" if len(v) > 1 else "n/a"),
            ("Readings/hour", lambda v, d: f"{len(v) / hours:.1f}"),
            ("Total readings", lambda v, d: f"{len(v)}"),
            ("Outliers dropped", lambda v, d: f"{d}"),
        ]

        for row_label, fmt in rows:
            line = f"  {row_label:<26}"
            for label in board_labels:
                values, dropped = board_data[label]
                if values:
                    line += f"{fmt(values, dropped):<{col_width}}"
                else:
                    line += f"{'(no data)':<{col_width}}"
            print(line)


def main():
    parser = argparse.ArgumentParser(description="Compare BLE RSSI between tracker boards")
    parser.add_argument("--hours", type=float, default=24, help="Hours of history to analyze (default: 24)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed per-device statistics")
    args = parser.parse_args()

    url, token = load_secrets()
    headers = {"Authorization": f"Bearer {token}"}

    entities = get_rssi_entities(url, headers)
    if not entities:
        print("No RSSI entities found. Are both BLE trackers connected to HA?")
        print("Expected entities like: sensor.ble_tracker_lywsd02mmc_rssi")
        sys.exit(1)

    print(f"Found {len(entities)} devices with RSSI sensors")
    all_data = fetch_all_history(entities, url, headers, args.hours)

    if args.verbose:
        print_verbose(entities, all_data, args.hours)
    else:
        print_compact(entities, all_data, args.hours)

    # Check coverage against full BLE device list
    ble_devices = get_ble_devices()
    validate_coverage(entities, ble_devices)


if __name__ == "__main__":
    main()
