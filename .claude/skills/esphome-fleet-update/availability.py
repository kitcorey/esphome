#!/usr/bin/env python3
"""Fleet availability filter for the esphome-fleet-update skill.

Reads `fleet/availability.yml` and partitions a list of YAML configs into
eligible-now vs skipped-for-reason, given the current date. Devices not
listed in the manifest default to `status: active` (always eligible).

Usage:
    python3 availability.py outdoor-christmas-lights.yml space-heater.yml [--date YYYY-MM-DD] [--json]

Exit codes:
    0 — partition rendered
    2 — bad input (e.g. unparsable --date)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "error: PyYAML not available. Run via `uv run python3 ...` or install with `pip install pyyaml`.\n"
    )
    sys.exit(2)

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "fleet" / "availability.yml"

MONTH_NAMES = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def yaml_key(path_arg: str) -> str:
    """Derive the manifest key from a YAML path or basename."""
    return Path(path_arg).stem


def next_active_date(today: dt.date, active_months: list[int]) -> dt.date | None:
    """First date >= today whose month is in active_months."""
    if not active_months:
        return None
    for offset in range(0, 366):
        candidate = today + dt.timedelta(days=offset)
        if candidate.month in active_months:
            return candidate
    return None


def classify(yaml_arg: str, manifest: dict, today: dt.date) -> dict:
    key = yaml_key(yaml_arg)
    entry = manifest.get(key)
    base = {"yaml": yaml_arg, "key": key}

    if not entry:
        return {**base, "status": "eligible", "reason": "active (default)"}

    status = entry.get("status", "active")
    note = entry.get("note", "") or ""

    if status == "active":
        return {**base, "status": "eligible", "reason": "active"}

    if status == "seasonal":
        active = entry.get("active_months", []) or []
        if today.month in active:
            return {**base, "status": "eligible", "reason": "seasonal — in season"}
        nxt = next_active_date(today, active)
        if nxt is None:
            return {**base, "status": "skipped", "reason": "seasonal — no active months configured"}
        days = (nxt - today).days
        reason = f"seasonal — next active {MONTH_NAMES[nxt.month]} ({nxt.isoformat()}, in {days} days)"
        return {
            **base,
            "status": "skipped",
            "skip_kind": "seasonal",
            "reason": reason,
            "next_active": nxt.isoformat(),
            "days_until_active": days,
        }

    if status in {"offline", "broken"}:
        suffix = f": {note}" if note else ""
        return {
            **base,
            "status": "skipped",
            "skip_kind": status,
            "reason": f"{status}{suffix}",
        }

    return {**base, "status": "skipped", "skip_kind": "unknown", "reason": f"unknown status: {status}"}


def render_markdown(results: list[dict], today: dt.date) -> str:
    eligible = [r for r in results if r["status"] == "eligible"]
    skipped = [r for r in results if r["status"] == "skipped"]
    lines = [f"# Availability ({today.isoformat()})", ""]
    lines.append(f"## Eligible ({len(eligible)})")
    if eligible:
        for r in eligible:
            lines.append(f"- {r['yaml']} — {r['reason']}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append(f"## Skipped ({len(skipped)})")
    if skipped:
        for r in skipped:
            lines.append(f"- {r['yaml']} — {r['reason']}")
    else:
        lines.append("- (none)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("yamls", nargs="+", help="YAML paths or basenames")
    parser.add_argument("--date", help="Override current date (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    args = parser.parse_args()

    if args.date:
        try:
            today = dt.date.fromisoformat(args.date)
        except ValueError as e:
            sys.stderr.write(f"error: bad --date: {e}\n")
            return 2
    else:
        today = dt.date.today()

    if MANIFEST.exists():
        with open(MANIFEST) as f:
            manifest = yaml.safe_load(f) or {}
    else:
        manifest = {}

    results = [classify(y, manifest, today) for y in args.yamls]

    if args.json:
        json.dump({"date": today.isoformat(), "results": results}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(results, today) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
