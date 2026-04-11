"""Compare BLE RSSI performance between wESP32 and Olimex BLE tracker boards.

Queries Home Assistant's REST API for RSSI sensor history and prints a
side-by-side comparison table.

Usage:
    uv run python compare_ble.py             # Last 24 hours
    uv run python compare_ble.py --hours 1   # Last hour
"""

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

BOARDS = {
    "ble_tracker": "wESP32 (chip)",
    "ble_tracker_olimex": "Olimex (external)",
}


def load_secrets():
    secrets_path = Path(__file__).parent / "secrets.yaml"
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
    for state in resp.json():
        eid = state["entity_id"]
        if "rssi" not in eid or not eid.startswith("sensor."):
            continue
        # Match entities belonging to our BLE tracker devices
        for board_prefix, board_label in BOARDS.items():
            if eid.startswith(f"sensor.{board_prefix}_"):
                # Extract the BLE device name (e.g., "lywsd02mmc" or "lywsd03mmc")
                suffix = eid[len(f"sensor.{board_prefix}_"):]
                # Strip "olimex_" prefix if present (Olimex sensor names are prefixed)
                device = suffix.replace("olimex_", "").replace("_rssi", "").upper()
                entities.setdefault(device, {})[board_label] = eid
    return entities


def get_history(url, headers, entity_id, hours):
    """Fetch sensor history for the given time window."""
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    params = {"filter_entity_id": entity_id, "minimal_response": "", "no_attributes": ""}
    resp = requests.get(
        f"{url}/api/history/period/{start}", headers=headers, params=params, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    if not data or not data[0]:
        return []
    values = []
    for entry in data[0]:
        try:
            values.append(float(entry["state"]))
        except (ValueError, KeyError):
            continue
    return values


def print_comparison(entities, url, headers, hours):
    board_labels = list(BOARDS.values())
    col_width = 22

    print(f"\nBLE RSSI Comparison (last {hours}h)")
    print("=" * (28 + col_width * len(board_labels)))
    header = " " * 28
    for label in board_labels:
        header += f"{label:<{col_width}}"
    print(header)
    print("-" * (28 + col_width * len(board_labels)))

    for device in sorted(entities):
        print(f"\n{device}")
        board_data = {}
        for label in board_labels:
            eid = entities[device].get(label)
            if eid:
                values = get_history(url, headers, eid, hours)
                board_data[label] = values
            else:
                board_data[label] = []

        rows = [
            ("Mean RSSI (dBm)", lambda v: f"{statistics.mean(v):.1f}"),
            ("Min / Max", lambda v: f"{min(v):.0f} / {max(v):.0f}"),
            ("Std Dev", lambda v: f"{statistics.stdev(v):.1f}" if len(v) > 1 else "n/a"),
            ("Readings/hour", lambda v: f"{len(v) / hours:.1f}"),
            ("Total readings", lambda v: f"{len(v)}"),
        ]

        for row_label, fmt in rows:
            line = f"  {row_label:<26}"
            for label in board_labels:
                values = board_data[label]
                if values:
                    line += f"{fmt(values):<{col_width}}"
                else:
                    line += f"{'(no data)':<{col_width}}"
            print(line)


def main():
    parser = argparse.ArgumentParser(description="Compare BLE RSSI between tracker boards")
    parser.add_argument("--hours", type=float, default=24, help="Hours of history to analyze (default: 24)")
    args = parser.parse_args()

    url, token = load_secrets()
    headers = {"Authorization": f"Bearer {token}"}

    entities = get_rssi_entities(url, headers)
    if not entities:
        print("No RSSI entities found. Are both BLE trackers connected to HA?")
        print("Expected entities like: sensor.ble_tracker_lywsd02mmc_rssi")
        sys.exit(1)

    print(f"Found RSSI entities for: {', '.join(sorted(entities))}")
    print_comparison(entities, url, headers, args.hours)


if __name__ == "__main__":
    main()
