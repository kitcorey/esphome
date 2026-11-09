#!/usr/bin/env python3
"""One command, one approval: compile and OTA-flash every ESPHome device that is behind.

Flow: plan -> approve -> compile -> flash non-critical -> flash critical -> report.

Unattended by design. The only prompts after the first are for devices marked
`critical: true` in fleet/availability.yml. Any compile failure, flash failure,
or device that doesn't report the target version within 120 s halts the run.

Usage (mise loads HA_URL / HA_TOKEN from .env):
    mise run fleet-update                       # whole fleet
    mise run fleet-update --plan                # show the table and stop
    mise run fleet-update --devices a.yml,b.yml # narrow the scope
    mise run fleet-update --compile-only        # stop after compiling; touches no device
    mise run fleet-update --yes --approve-critical garage-door.yml

Without a TTY, critical devices that aren't pre-approved are reported as pending.

Exit codes:
    0   finished; pending critical devices don't count as failure
    1   halted, or something failed (HA unreachable, compile, flash, restore)
    2   bad input or missing credentials
    130 interrupted; the report is still written
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILLS / "esphome-release-review"))  # inventory
sys.path.insert(0, str(SKILLS / "esphome-fleet-update"))  # availability

import inventory  # noqa: E402
import availability  # noqa: E402

REPO = inventory.REPO
FLASH_TIMEOUT_S = 300
VERSION_TIMEOUT_S = 120
VERSION_POLL_S = 5
RESTORE_TIMEOUT_S = 30
RESTORE_POLL_S = 3
LOG_TAIL_LINES = 40

STATIC_IP_RE = re.compile(r"^\s*static_ip:\s*['\"]?(\d+\.\d+\.\d+\.\d+)", re.M)
CHIP_ORDER = {"esp32": 0, None: 1, "esp8266": 2}
HA_ERRORS = (urllib.error.URLError, TimeoutError, OSError, ValueError)

# Device-level state worth seeing before approving a critical flash.
STATE_DOMAINS = ("climate", "cover", "fan", "light", "lock", "switch")


class HaltRun(Exception):
    """Stop flashing. The message has already been recorded in the report."""


@dataclasses.dataclass
class Item:
    yml: str
    action: str  # flash | up to date | unavailable | unreachable
    note: str = ""
    critical: bool = False
    current: str | None = None
    ip: str | None = None
    chip: str | None = None
    device_id: str | None = None


@dataclasses.dataclass
class Report:
    target: str
    log_dir: Path
    interrupted: bool = False
    compiled: list[str] = dataclasses.field(default_factory=list)
    flashed: list[str] = dataclasses.field(default_factory=list)
    restored: list[str] = dataclasses.field(default_factory=list)
    skipped: list[str] = dataclasses.field(default_factory=list)
    pending: list[str] = dataclasses.field(default_factory=list)
    failed: list[str] = dataclasses.field(default_factory=list)
    not_flashed: list[str] = dataclasses.field(default_factory=list)
    not_touched: list[str] = dataclasses.field(default_factory=list)

    def render(self) -> str:
        sections = [
            ("Failed", self.failed),
            ("Not flashed (run stopped first)", self.not_flashed),
            ("Pending critical (needs approval)", self.pending),
            ("Flashed", self.flashed),
            ("Switches turned back on", self.restored),
            ("Compiled", self.compiled),
            ("Skipped by prompt", self.skipped),
            ("Not in this run", self.not_touched),
        ]
        lines = [f"# Fleet update to {self.target}", "", f"Logs: {self.log_dir}"]
        if self.interrupted:
            lines += ["", "**Interrupted before finishing.**"]
        for title, entries in sections:
            if entries:
                lines += ["", f"## {title} ({len(entries)})", *(f"- {e}" for e in entries)]
        return "\n".join(lines) + "\n"


class Ha:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def template(self, template: str) -> str:
        return inventory.http_post_json(f"{self.url}/api/template", self.token, {"template": template})

    def turn_on(self, entity: str) -> None:
        inventory.http_post_json(f"{self.url}/api/services/switch/turn_on", self.token, {"entity_id": entity})

    def is_on(self, entity: str) -> bool:
        return self.template(f"{{{{ states({json.dumps(entity)}) }}}}").strip() == "on"

    def version(self, device_id: str) -> str | None:
        raw = self.template(f"{{{{ device_attr({json.dumps(device_id)}, 'sw_version') }}}}")
        return inventory.parse_sw_version(raw)[0]

    def entity_states(self, device_id: str) -> dict[str, str]:
        """Current state of each device entity, as {entity_id: state}."""
        raw = self.template(
            "{%- set ns = namespace(out={}) -%}"
            f"{{%- for e in device_entities({json.dumps(device_id)}) -%}}"
            "{%- set ns.out = dict(ns.out, **{e: states(e)}) -%}"
            "{%- endfor -%}"
            "{{ ns.out | to_json }}"
        )
        return json.loads(raw)

    def switches_on(self, device_id: str) -> list[str]:
        states = self.entity_states(device_id)
        return sorted(e for e, s in states.items() if e.startswith("switch.") and s == "on")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def version_key(v: str | None) -> tuple[int, ...]:
    if not v:
        return ()
    return tuple(int(p) if p.isdigit() else 0 for p in v.split("."))


def ping(ip: str) -> bool:
    for _ in range(2):
        if subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True).returncode == 0:
            return True
    return False


def run_logged(cmd: list[str], log: Path, timeout: int | None = None) -> int | None:
    """Run cmd with output to log. Returns the exit code, or None on timeout.

    The child gets its own session so a timeout or Ctrl-C kills `uv` and the
    esphome process under it together.
    """
    with open(log, "ab") as f:
        f.write(f"$ {' '.join(cmd)}\n".encode())
        f.flush()
        proc = subprocess.Popen(cmd, cwd=REPO, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            return None
        except BaseException:
            kill_group(proc)
            raise


def kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    except ProcessLookupError:
        pass


def show_log_tail(log: Path) -> None:
    lines = log.read_text(errors="replace").splitlines()
    say(f"--- last {LOG_TAIL_LINES} lines of {log} ---")
    say("\n".join(lines[-LOG_TAIL_LINES:]))
    say("---")


def get_target() -> str:
    out = subprocess.run(["uv", "run", "esphome", "version"], cwd=REPO, capture_output=True, text=True)
    m = re.search(r"Version:\s*(\S+)", out.stdout)
    if out.returncode != 0 or not m:
        raise RuntimeError(f"could not read esphome version: {out.stdout}{out.stderr}".strip())
    return m.group(1)


def load_manifest() -> dict:
    if not availability.MANIFEST.exists():
        return {}
    with open(availability.MANIFEST) as f:
        return availability.yaml.safe_load(f) or {}


def resolve_names(raw: str | None, known: set[str]) -> list[str]:
    """Turn a comma-separated flag value into repo YAML basenames, or raise ValueError."""
    if not raw:
        return []
    names = [Path(n.strip()).name for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"not a device YAML in this repo: {', '.join(unknown)}")
    return names


def build_plan(ha: Ha, target: str, today: dt.date, only: list[str]) -> list[Item]:
    devices = inventory.fetch_devices(ha.url, ha.token)
    ids = {d.get("name_by_user") or d.get("name") or "?": d["id"] for d in devices}
    rows: dict[str, list[dict]] = {}
    for row in inventory.build_rows(devices, REPO):
        if row["yaml"]:
            rows.setdefault(row["yaml"], []).append(row)

    manifest = load_manifest()
    items = []
    for entry in inventory.index_yamls(REPO):
        yml = str(entry["path"].relative_to(REPO))
        if only and yml not in only:
            continue
        items.append(classify(yml, entry, rows.get(yml, []), ids, manifest, today, target))
    return items


def classify(yml, entry, matches, ids, manifest, today, target) -> Item:
    match = matches[0] if len(matches) == 1 else None
    item = Item(
        yml=yml,
        action="unavailable",
        critical=bool((manifest.get(Path(yml).stem) or {}).get("critical")),
        current=match["esphome_version"] if match else None,
        chip=(match and match["chip"]) or entry["chip"],
    )

    avail = availability.classify(yml, manifest, today)
    if avail["status"] != "eligible":
        item.note = avail["reason"]
        return item

    # Flashing is only safe if HA can confirm the result, so a YAML needs exactly one device.
    if not matches:
        item.note = "no matching HA device"
        return item
    if len(matches) > 1:
        titles = ", ".join(m["title"] for m in matches)
        item.note = f"matches several HA devices: {titles}"
        return item
    item.device_id = ids[match["title"]]

    if item.current and version_key(item.current) >= version_key(target):
        item.action = "up to date"
        return item

    ip = STATIC_IP_RE.search(entry["path"].read_text(errors="replace"))
    if not ip:
        item.action = "unreachable"
        item.note = "no static_ip in YAML"
        return item
    item.ip = ip.group(1)
    if not ping(item.ip):
        item.action = "unreachable"
        item.note = f"no ping reply from {item.ip}"
        return item

    item.action = "flash"
    return item


def flash_order(items: list[Item]) -> list[Item]:
    queue = [i for i in items if i.action == "flash"]
    return sorted(queue, key=lambda i: (i.critical, CHIP_ORDER.get(i.chip, 1), i.yml))


def print_plan(items: list[Item], target: str, approved: set[str], interactive: bool) -> None:
    def action_label(i: Item) -> str:
        if i.action != "flash" or not i.critical:
            return i.action
        if i.yml in approved:
            return "flash (critical, approved)"
        return "flash (critical, will ask)" if interactive else "flash (critical, pending)"

    groups = {"flash": 0, "unreachable": 1, "unavailable": 2, "up to date": 3}
    queue = flash_order(items)
    rest = sorted((i for i in items if i.action != "flash"), key=lambda i: (groups[i.action], i.yml))
    table = [("YAML", "Action", "Current", "Note")]
    table += [(i.yml, action_label(i), i.current or "?", i.note or (i.ip or "")) for i in queue + rest]
    widths = [max(len(row[col]) for row in table) for col in range(4)]

    say(f"Target: ESPHome {target}\n")
    for row in table:
        say("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    say(f"\n{len(queue)} to flash, {sum(i.critical for i in queue)} of them critical (flashed last).")


def compile_all(queue: list[Item], report: Report) -> None:
    for n, item in enumerate(queue, 1):
        say(f"[compile {n}/{len(queue)}] {item.yml}")
        log = report.log_dir / f"{Path(item.yml).stem}.compile.log"
        started = time.monotonic()
        rc = run_logged(["uv", "run", "esphome", "compile", item.yml], log)
        if rc != 0:
            show_log_tail(log)
            report.failed.append(f"{item.yml}: compile failed (exit {rc}), see {log}")
            raise HaltRun
        report.compiled.append(f"{item.yml} ({time.monotonic() - started:.0f} s)")


def wait_for_version(ha: Ha, item: Item, target: str) -> str | None:
    """Poll HA until the device reports the target. Returns the last version seen on timeout."""
    deadline = time.monotonic() + VERSION_TIMEOUT_S
    last = None
    while True:
        try:
            last = ha.version(item.device_id) or last
        except HA_ERRORS:
            pass  # HA hiccups are tolerated until the deadline
        if last and version_key(last) >= version_key(target):
            return None
        if time.monotonic() >= deadline:
            return last or "unknown"
        time.sleep(VERSION_POLL_S)


def restore_switch(ha: Ha, entity: str) -> str | None:
    """Turn a switch on and confirm it. Returns an error string on failure.

    HA silently drops service calls to entities that are still unavailable right
    after reconnecting, so a successful call alone proves nothing.
    """
    deadline = time.monotonic() + RESTORE_TIMEOUT_S
    error = "state never became on"
    while time.monotonic() < deadline:
        try:
            ha.turn_on(entity)
            time.sleep(RESTORE_POLL_S)
            if ha.is_on(entity):
                return None
            error = "state never became on"
        except HA_ERRORS as e:
            error = str(e)
            time.sleep(RESTORE_POLL_S)
    return error


def flash_one(ha: Ha, item: Item, target: str, report: Report, in_flight: dict) -> None:
    say(f"[flash] {item.yml} -> {item.ip} ({item.current or '?'} -> {target})")
    try:
        switches = ha.switches_on(item.device_id)
    except HA_ERRORS as e:
        report.failed.append(f"{item.yml}: could not read switch states from HA, not flashed: {e}")
        raise HaltRun from e

    in_flight.update(yml=item.yml, switches=switches)
    unrestored = f"; these were on and were not turned back on: {', '.join(switches)}" if switches else ""
    log = report.log_dir / f"{Path(item.yml).stem}.flash.log"
    started = time.monotonic()

    rc = run_logged(
        ["uv", "run", "esphome", "run", item.yml, "--device", item.ip, "--no-logs"],
        log,
        timeout=FLASH_TIMEOUT_S,
    )
    if rc != 0:
        show_log_tail(log)
        why = f"timed out after {FLASH_TIMEOUT_S} s" if rc is None else f"exit {rc}"
        report.failed.append(f"{item.yml}: flash failed ({why}), see {log}{unrestored}")
        in_flight.clear()
        raise HaltRun

    seen = wait_for_version(ha, item, target)
    if seen is not None:
        report.failed.append(
            f"{item.yml}: flashed, but HA still reports {seen} after {VERSION_TIMEOUT_S} s "
            f"(target {target}), see {log}{unrestored}"
        )
        in_flight.clear()
        raise HaltRun

    report.flashed.append(f"{item.yml}: {item.current or '?'} -> {target} ({time.monotonic() - started:.0f} s)")
    say(f"  ok, HA reports {target}")

    for entity in switches:
        error = restore_switch(ha, entity)
        if error:
            # Reported, not fatal: the device itself is healthy.
            report.failed.append(f"{item.yml}: could not turn {entity} back on: {error}")
            say(f"  FAILED to turn {entity} back on: {error}")
        else:
            report.restored.append(f"{entity} ({item.yml})")
            say(f"  turned {entity} back on")
    in_flight.clear()


def ask_critical(ha: Ha, item: Item, target: str) -> str:
    """Prompt for one critical device. Returns 'y' or 'skip'."""
    say(f"\n[critical] {item.yml} -> {item.ip}")
    say(f"  Version: {item.current or '?'} -> {target}")
    try:
        states = ha.entity_states(item.device_id)
        shown = {e: s for e, s in sorted(states.items()) if e.split(".")[0] in STATE_DOMAINS}
        for entity, state in shown.items():
            say(f"  {entity}: {state}")
    except HA_ERRORS as e:
        say(f"  (could not read current state from HA: {e})")
    while True:
        answer = input("  Flash now? [y/skip] ").strip().lower()
        if answer in {"y", "yes"}:
            return "y"
        if answer in {"s", "skip"}:
            return "skip"


def flash_all(
    ha: Ha, queue: list[Item], target: str, report: Report, approved: set[str], interactive: bool, in_flight: dict
) -> None:
    for pos, item in enumerate(queue):
        report.not_flashed = [i.yml for i in queue[pos:]]
        if item.critical and item.yml not in approved:
            if not interactive:
                report.pending.append(f"{item.yml}: {item.current or '?'} -> {target}")
                continue
            try:
                answer = ask_critical(ha, item, target)
            except EOFError:
                report.pending.append(f"{item.yml}: {item.current or '?'} -> {target}")
                continue
            if answer == "skip":
                report.skipped.append(item.yml)
                continue
        # From here on a failure is reported under Failed, not as untouched.
        report.not_flashed = [i.yml for i in queue[pos + 1 :]]
        flash_one(ha, item, target, report, in_flight)
    report.not_flashed = []


def finish(report: Report, stamp: str) -> None:
    """Write the file first, in case stdout has gone away with the terminal."""
    text = report.render()
    path = Path(f"/tmp/fleet-update-{stamp}.md")
    path.write_text(text)
    say("\n" + text)
    say(f"Report: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--devices", help="Comma-separated YAMLs to narrow the scope to")
    parser.add_argument("--plan", action="store_true", help="Print the plan table and stop")
    parser.add_argument("--compile-only", action="store_true", help="Stop after compiling; touches no device")
    parser.add_argument("--yes", action="store_true", help="Skip the Proceed prompt")
    parser.add_argument("--approve-critical", help="Comma-separated critical YAMLs to flash without a prompt")
    parser.add_argument("--date", help="Override current date for availability (YYYY-MM-DD)")
    args = parser.parse_args()

    try:
        today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
        known = {str(e["path"].relative_to(REPO)) for e in inventory.index_yamls(REPO)}
        only = resolve_names(args.devices, known)
        approved = set(resolve_names(args.approve_critical, known))
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    ha_url = os.environ.get("HA_URL", "").strip()
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    if not ha_url or not ha_token:
        sys.stderr.write("error: set HA_URL and HA_TOKEN (`mise run fleet-update` loads them from .env).\n")
        return 2
    ha = Ha(ha_url, ha_token)
    interactive = sys.stdin.isatty()

    try:
        target = get_target()
        items = build_plan(ha, target, today, only)
    except RuntimeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    except HA_ERRORS as e:
        sys.stderr.write(f"error: could not query HA at {ha_url}: {e}\n")
        return 1

    print_plan(items, target, approved, interactive)
    queue = flash_order(items)
    if args.plan:
        return 0
    if not queue:
        say("Nothing to do.")
        return 0

    # --compile-only touches no device, so it needs no approval.
    if not args.yes and not args.compile_only:
        if not interactive:
            sys.stderr.write("error: no TTY to ask on. Review with --plan, then re-run with --yes.\n")
            return 2
        if input("Proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
            say("Aborted, nothing changed.")
            return 0

    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    log_dir = Path(f"/tmp/fleet-update-{stamp}")
    log_dir.mkdir()
    report = Report(target=target, log_dir=log_dir)
    report.not_touched = [
        f"{i.yml}: {i.action}{f' ({i.note})' if i.note else ''}" for i in items if i.action != "flash"
    ]

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)

    # Don't spend compile time on critical devices nobody can approve in this run.
    pending_only = {i.yml for i in queue if i.critical and i.yml not in approved and not interactive}
    to_compile = queue if args.compile_only else [i for i in queue if i.yml not in pending_only]

    in_flight: dict = {}
    code = 0
    try:
        report.not_flashed = [] if args.compile_only else [i.yml for i in queue]
        compile_all(to_compile, report)
        if not args.compile_only:
            flash_all(ha, queue, target, report, approved, interactive, in_flight)
    except HaltRun:
        code = 1
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        code = 130
        report.interrupted = True
        if in_flight:
            switches = ", ".join(in_flight["switches"]) or "none"
            report.failed.append(
                f"{in_flight['yml']}: interrupted mid-flash, device state unknown; "
                f"switches that were on before: {switches}"
            )
    if report.failed and code == 0:
        code = 1
    finish(report, stamp)
    return code


if __name__ == "__main__":
    sys.exit(main())
