#!/usr/bin/env python3
"""Fleet inventory for the esphome-release-review skill.

Queries Home Assistant via REST (`/api/template`) for every ESPHome integration
device, parses `sw_version` into an ESPHome version + bundled-firmware flag,
and matches each device to a YAML in this repo by `substitutions.name`.

Portable: requires only HA_URL + HA_TOKEN (long-lived access token from
/profile/security). Works from any machine that can reach HA. The repo path is
derived from this script's location, so it works from any clone of the repo.

Usage:
    python3 inventory.py            # markdown table
    python3 inventory.py --json     # JSON array
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

DEVICE_TEMPLATE = """
{%- set ns = namespace(seen=[], rows=[]) -%}
{%- for ent in integration_entities('esphome') -%}
  {%- set did = device_id(ent) -%}
  {%- if did and did not in ns.seen -%}
    {%- set ns.seen = ns.seen + [did] -%}
    {%- set ns.rows = ns.rows + [{
      "id": did,
      "name": device_attr(did, 'name'),
      "name_by_user": device_attr(did, 'name_by_user'),
      "manufacturer": device_attr(did, 'manufacturer'),
      "model": device_attr(did, 'model'),
      "sw_version": device_attr(did, 'sw_version'),
      "disabled_by": device_attr(did, 'disabled_by'),
    }] -%}
  {%- endif -%}
{%- endfor -%}
{{ ns.rows | to_json }}
""".strip()

# sw_version comes in two shapes:
#   pure:    "2026.4.3 (2026-05-08 03:25:18 +0000)"
#   bundled: "5.3.3 (ESPHome 2025.12.4)"  /  "26.4.0 (ESPHome 2026.3.2)"
BUNDLED_RE = re.compile(r"\(ESPHome\s+([\d.]+)\s*\)")
PURE_RE = re.compile(r"^\s*([\d.]+)\s*\(")

YAML_NAME_RE = re.compile(r"^\s*name:\s*['\"]?([A-Za-z0-9._-]+)['\"]?\s*$", re.M)
YAML_FRIENDLY_RE = re.compile(r"^\s*friendly_name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", re.M)
FRAMEWORK_RE = re.compile(r"^\s*type:\s*([A-Za-z0-9_-]+)\s*$", re.M)
ESP32_BLOCK_RE = re.compile(r"^esp32:", re.M)
ESP8266_BLOCK_RE = re.compile(r"^esp8266:", re.M)


def http_post_json(url: str, token: str, body: dict, timeout: int = 15) -> str:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    ctx = ssl.create_default_context()
    if os.environ.get("HA_INSECURE_TLS") == "1":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode()


def fetch_devices(ha_url: str, token: str) -> list[dict]:
    raw = http_post_json(
        f"{ha_url.rstrip('/')}/api/template",
        token,
        {"template": DEVICE_TEMPLATE},
    )
    # /api/template returns the rendered string verbatim (not JSON-wrapped).
    return json.loads(raw)


def parse_sw_version(sw: str | None) -> tuple[str | None, bool, str | None]:
    """Returns (esphome_version, bundled, vendor_version)."""
    if not sw:
        return None, False, None
    m = BUNDLED_RE.search(sw)
    if m:
        vendor = sw.split("(")[0].strip() or None
        return m.group(1), True, vendor
    m = PURE_RE.match(sw)
    if m:
        return m.group(1), False, None
    return None, False, None


def slug(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[\s_]+", "-", s.strip().lower())


def chip_family(model: str | None) -> str | None:
    if not model:
        return None
    m = model.lower()
    if m.startswith("esp32") or "esp32" in m:
        return "esp32"
    if m.startswith("esp8266") or m in {"esp12e", "esp12f", "esp01_1m", "esp01"}:
        return "esp8266"
    return None


def index_yamls(repo: Path) -> list[dict]:
    """Read each top-level YAML and pull out the matching identifiers."""
    out = []
    for path in sorted(list(repo.glob("*.yml")) + list(repo.glob("*.yaml"))):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        sub_idx = text.find("substitutions:")
        if sub_idx == -1:
            continue
        snippet = text[sub_idx : sub_idx + 800]
        name = YAML_NAME_RE.search(snippet)
        friendly = YAML_FRIENDLY_RE.search(snippet)
        chip, framework = detect_framework(path, repo)
        out.append(
            {
                "path": path,
                "name": name.group(1).lower() if name else None,
                "friendly": friendly.group(1).strip() if friendly else None,
                "chip": chip,
                "framework": framework,
            }
        )
    return out


def match_yaml(device: dict, index: list[dict]) -> dict | None:
    """Find the YAML for a device, disambiguating duplicates by chip family."""
    candidates = []
    targets = {
        slug(device.get("name")),
        slug(device.get("name_by_user")),
    }
    targets.discard("")
    for entry in index:
        entry_slugs = {entry["name"], slug(entry["friendly"])}
        entry_slugs.discard(None)
        entry_slugs.discard("")
        if entry_slugs & targets:
            candidates.append(entry)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Disambiguate by chip family from the device's model.
    want_chip = chip_family(device.get("model"))
    if want_chip:
        narrowed = [c for c in candidates if c["chip"] == want_chip]
        if len(narrowed) == 1:
            return narrowed[0]
        if narrowed:
            return narrowed[0]
    return candidates[0]


def detect_framework(yaml_path: Path | None, repo: Path) -> tuple[str | None, str | None]:
    """Return (chip, framework) from YAML + its common/ includes."""
    if not yaml_path or not yaml_path.exists():
        return None, None
    text = yaml_path.read_text(errors="replace")
    # Pull in common/*.yml that this YAML !includes.
    for inc in re.findall(r"!include\s+(common/[\w.-]+)", text):
        p = repo / inc
        if p.exists():
            text += "\n" + p.read_text(errors="replace")
    chip = None
    if ESP32_BLOCK_RE.search(text):
        chip = "esp32"
    elif ESP8266_BLOCK_RE.search(text):
        chip = "esp8266"
    framework = None
    # Look for `framework:` block, then `type:` within ~5 lines.
    for m in re.finditer(r"^\s*framework:\s*$", text, re.M):
        rest = text[m.end() : m.end() + 200]
        f = FRAMEWORK_RE.search(rest)
        if f:
            framework = f.group(1)
            break
    if framework is None and chip == "esp8266":
        framework = "arduino"  # esp8266 only supports arduino
    return chip, framework


def build_rows(devices: list[dict], repo: Path) -> list[dict]:
    index = index_yamls(repo)
    rows = []
    for d in devices:
        ev, project_pattern, vendor = parse_sw_version(d.get("sw_version"))
        title = d.get("name_by_user") or d.get("name") or "?"
        match = match_yaml(d, index)
        yaml_path = match["path"] if match else None
        chip = (match and match["chip"]) or chip_family(d.get("model"))
        framework = match["framework"] if match else None
        # vendor_managed: firmware uses esphome.project metadata AND we have no
        # YAML for it. With a YAML, the user owns the build (e.g. mahtanar fork
        # via mitsubishi-huzzah32.yaml emits the same `(ESPHome X.Y.Z)` shape).
        vendor_managed = project_pattern and yaml_path is None
        rows.append(
            {
                "title": title,
                "esphome_version": ev,
                "vendor_managed": vendor_managed,
                "vendor_version": vendor if project_pattern else None,
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "chip": chip,
                "framework": framework,
                "yaml": str(yaml_path.relative_to(repo)) if yaml_path else None,
                "sw_version_raw": d.get("sw_version"),
            }
        )
    rows.sort(key=lambda r: (r["vendor_managed"], r["esphome_version"] or "", r["title"].lower()))
    return rows


def render_markdown(rows: list[dict]) -> str:
    lines = [
        "| Device | ESPHome | Source | Chip | Framework | YAML | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        notes = []
        if r["vendor_version"]:
            notes.append(f"project version {r['vendor_version']}")
        if r["yaml"] is None and not r["vendor_managed"]:
            notes.append("no YAML matched")
        source = "vendor" if r["vendor_managed"] else "repo"
        lines.append(
            "| {title} | {ev} | {src} | {chip} | {fw} | {yaml} | {notes} |".format(
                title=r["title"],
                ev=r["esphome_version"] or "?",
                src=source,
                chip=r["chip"] or "?",
                fw=r["framework"] or "?",
                yaml=r["yaml"] or "—",
                notes="; ".join(notes),
            )
        )
    return "\n".join(lines)


def main() -> int:
    ha_url = os.environ.get("HA_URL", "").strip()
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    if not ha_url or not ha_token:
        sys.stderr.write(
            "error: set HA_URL (e.g. http://homeassistant.local:8123) "
            "and HA_TOKEN (long-lived access token from /profile/security).\n"
        )
        return 2
    try:
        devices = fetch_devices(ha_url, ha_token)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"error: HA returned {e.code} {e.reason} for /api/template\n")
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        sys.stderr.write(f"error: could not reach HA at {ha_url}: {e}\n")
        return 1
    rows = build_rows(devices, REPO)
    if "--json" in sys.argv[1:]:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(rows) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
