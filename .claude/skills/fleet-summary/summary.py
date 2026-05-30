#!/usr/bin/env python3
"""One-shot fleet snapshot for the fleet-summary skill.

Deterministic "what is my ESPHome fleet doing right now" report: every device's
online state, installed ESPHome version, matched repo YAML, Source (repo/vendor),
and whether its current liveness *matches expectations* (a seasonal device down
in the off-season is fine; an active device offline is a flag).

This is a thin orchestrator. It reuses the two sibling skills' battle-tested
helpers verbatim rather than reimplementing them:
  - inventory.py (esphome-release-review): HA query + version parse + YAML match.
  - availability.py (esphome-fleet-update): seasonal/offline/broken expectation.

The only new logic here is the liveness query, the liveness x expectation
discrepancy classification, within-fleet version drift, and the reverse map of
repo YAMLs that match no live device.

Within-fleet drift only — no upstream/network version fetch (that's
esphome-release-review's job). Inline output only — no /tmp artifact.

Usage:
    uv run python3 summary.py            # markdown table
    uv run python3 summary.py --json     # structured JSON
    uv run python3 summary.py --date 2026-12-15   # test seasonal expectation

Exit codes (mirror inventory.py):
    0 — snapshot rendered
    1 — HA unreachable / HTTP error
    2 — missing creds or bad input
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILLS / "esphome-release-review"))  # inventory
sys.path.insert(0, str(SKILLS / "esphome-fleet-update"))  # availability

import inventory  # noqa: E402
import availability  # noqa: E402

# inventory.DEVICE_TEMPLATE's row dict, plus an `online` bool computed inline per
# device: online == any device entity is not unavailable/unknown.
SUMMARY_TEMPLATE = """
{%- set ns = namespace(seen=[], rows=[]) -%}
{%- for ent in integration_entities('esphome') -%}
  {%- set did = device_id(ent) -%}
  {%- if did and did not in ns.seen -%}
    {%- set ns.seen = ns.seen + [did] -%}
    {%- set up = namespace(v=false) -%}
    {%- for e in device_entities(did) -%}{%- if states(e) not in ['unavailable','unknown'] -%}{%- set up.v = true -%}{%- endif -%}{%- endfor -%}
    {%- set ns.rows = ns.rows + [{
      "id": did,
      "name": device_attr(did, 'name'),
      "name_by_user": device_attr(did, 'name_by_user'),
      "manufacturer": device_attr(did, 'manufacturer'),
      "model": device_attr(did, 'model'),
      "sw_version": device_attr(did, 'sw_version'),
      "disabled_by": device_attr(did, 'disabled_by'),
      "online": up.v,
    }] -%}
  {%- endif -%}
{%- endfor -%}
{{ ns.rows | to_json }}
""".strip()


def fetch_devices_with_liveness(ha_url: str, token: str) -> list[dict]:
    raw = inventory.http_post_json(
        f"{ha_url.rstrip('/')}/api/template",
        token,
        {"template": SUMMARY_TEMPLATE},
    )
    return json.loads(raw)


def device_title(d: dict) -> str:
    """Same key build_rows derives as `title` — used to join liveness back in."""
    return d.get("name_by_user") or d.get("name") or "?"


def version_key(v: str | None) -> tuple[int, ...]:
    if not v:
        return ()
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def classify_state(row: dict, manifest: dict, today: dt.date) -> tuple[str, str]:
    """Liveness x expectation discrepancy classification. Returns (state, note)."""
    online = row.get("online", False)

    if row["vendor_managed"]:
        note = f"project version {row['vendor_version']}" if row.get("vendor_version") else "vendor firmware channel"
        return "vendor (separate channel)", note

    if not row["yaml"]:
        return "unmanaged (no repo YAML)", ""

    exp = availability.classify(row["yaml"], manifest, today)

    if exp["status"] == "eligible":
        # active, or seasonal and currently in season — expected to be up.
        if online:
            return "ok", ""
        return "⚠ unexpectedly offline", exp["reason"]

    # exp["status"] == "skipped" — manifest expects this device down.
    kind = exp.get("skip_kind", "unknown")
    if not online:
        return "expected down", exp["reason"]
    # Online but the manifest says it should be down.
    if kind == "seasonal":
        # Plugged in early / left up — informational, not a flag.
        return "on (out of season)", exp["reason"]
    return "⚠ manifest stale", f"marked {kind}, but online — {exp['reason']}"


def build_summary(devices: list[dict], today: dt.date) -> dict:
    rows = inventory.build_rows(devices, inventory.REPO)

    # Join liveness back by title (build_rows drops the extra `online` key).
    live = {device_title(d): d.get("online", False) for d in devices}

    if availability.MANIFEST.exists():
        with open(availability.MANIFEST) as f:
            manifest = availability.yaml.safe_load(f) or {}
    else:
        manifest = {}

    out_rows = []
    for r in rows:
        online = live.get(r["title"], False)
        merged = {**r, "online": online}
        state, note = classify_state(merged, manifest, today)
        out_rows.append(
            {
                "title": r["title"],
                "state": state,
                "online": online,
                "esphome_version": r["esphome_version"],
                "source": "vendor" if r["vendor_managed"] else "repo",
                "yaml": r["yaml"],
                "note": note,
            }
        )

    # Within-fleet drift: highest ESPHome version among Source=repo devices,
    # and which lag it.
    repo_versioned = [r for r in out_rows if r["source"] == "repo" and r["esphome_version"]]
    drift = {"highest": None, "laggards": []}
    if repo_versioned:
        highest = max(repo_versioned, key=lambda r: version_key(r["esphome_version"]))["esphome_version"]
        drift["highest"] = highest
        drift["laggards"] = [
            {"title": r["title"], "esphome_version": r["esphome_version"]}
            for r in repo_versioned
            if version_key(r["esphome_version"]) < version_key(highest)
        ]

    # Reverse map: repo YAMLs (with substitutions:) matching no live device.
    matched_yamls = {r["yaml"] for r in out_rows if r["yaml"]}
    unmatched = []
    for entry in inventory.index_yamls(inventory.REPO):
        rel = str(entry["path"].relative_to(inventory.REPO))
        if rel not in matched_yamls:
            unmatched.append(rel)

    total = len(out_rows)
    online_n = sum(1 for r in out_rows if r["online"])
    expected_n = sum(1 for r in out_rows if r["state"] == "expected down")
    flags_n = sum(1 for r in out_rows if r["state"].startswith("⚠"))

    rollup = {
        "date": today.isoformat(),
        "devices": total,
        "online": online_n,
        "down": total - online_n,
        "down_expected": expected_n,
        "flags": flags_n,
    }

    return {
        "rollup": rollup,
        "rows": out_rows,
        "drift": drift,
        "unmatched_configs": unmatched,
    }


def render_markdown(summary: dict) -> str:
    rows = summary["rows"]
    roll = summary["rollup"]
    drift = summary["drift"]

    lines = [
        "| Device | State | Online | ESPHome | Source | YAML | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            "| {title} | {state} | {online} | {ev} | {src} | {yaml} | {note} |".format(
                title=r["title"],
                state=r["state"],
                online="●" if r["online"] else "○",
                ev=r["esphome_version"] or "?",
                src=r["source"],
                yaml=r["yaml"] or "—",
                note=r["note"],
            )
        )

    rollup_line = (
        f"\n**{roll['devices']} devices:** {roll['online']} online, "
        f"{roll['down']} down ({roll['down_expected']} expected), {roll['flags']} flags"
    )
    lines.append(rollup_line)

    if drift["highest"]:
        if drift["laggards"]:
            lag = ", ".join(f"{d['title']} ({d['esphome_version']})" for d in drift["laggards"])
            lines.append(f"**Drift:** highest repo version {drift['highest']}; lagging: {lag}")
        else:
            lines.append(f"**Drift:** all repo devices on {drift['highest']}")

    if summary["unmatched_configs"]:
        lines.append("**Configs with no live device:** " + ", ".join(summary["unmatched_configs"]))

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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

    ha_url = os.environ.get("HA_URL", "").strip()
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    if not ha_url or not ha_token:
        sys.stderr.write(
            "error: set HA_URL (e.g. http://homeassistant.local:8123) "
            "and HA_TOKEN (long-lived access token from /profile/security).\n"
        )
        return 2

    try:
        devices = fetch_devices_with_liveness(ha_url, ha_token)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"error: HA returned {e.code} {e.reason} for /api/template\n")
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        sys.stderr.write(f"error: could not reach HA at {ha_url}: {e}\n")
        return 1

    summary = build_summary(devices, today)

    if args.json:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
