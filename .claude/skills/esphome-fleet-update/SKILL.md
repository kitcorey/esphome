---
name: esphome-fleet-update
description: Compile and OTA-flash a set of ESPHome devices, driven by a prior `esphome-release-review` report or an explicit device list. Filters out seasonal/offline/broken devices via `fleet/availability.yml`, compiles everything up front, pings each device for reachability, then walks the user through OTA flashes one device at a time with explicit per-device confirmation. Stops on any compile or flash failure. Use when the user says "flash the affected devices", "update the fleet", "run the fleet update", "OTA the devices from the review", "/esphome-fleet-update".
---

# ESPHome fleet update

Sibling to `esphome-release-review`. The review skill is read-only — it produces a report. This skill is the write side: recompile and OTA the devices the review identified, with the user in the loop for every flash.

The skill never edits YAML, never edits `fleet/availability.yml`, and never batch-flashes without per-device confirmation.

## Inputs

- `--review <path>` — markdown report from a prior `esphome-release-review`. Parse the affected-YAMLs list from the breaking-changes table and the **High value** section.
- `--devices <yml,yml,...>` — explicit list of device YAMLs (basenames or paths).
- No arg — invoke `esphome-release-review` first against latest stable, then proceed against its **High value** set. If the review produces nothing actionable, stop with a one-liner.

## Prerequisites

- **HA credentials** — same as `esphome-release-review`. `HA_URL` + `HA_TOKEN` in the shell, optionally `HA_INSECURE_TLS=1`. Used to call `inventory.py` for the device/Source/Framework cross-reference and the post-flash `sw_version` re-check.
- **`uv`** — every esphome invocation goes through `uv run esphome ...`, never bare `esphome`. (See `feedback_uv.md`.)
- **Repo working tree** — devices are flashed from this repo's top-level YAMLs; the user's pwd should be `/home/kit/repos/esphome`.

## Workflow — seven steps

### Step 1: Resolve scope

Build a YAML list from `--review`, `--devices`, or by running `esphome-release-review` and pulling its **High value** set. Deduplicate.

Surface the source so the user knows what they're acting on: "From review of 2026.5.0: 4 YAMLs flagged."

### Step 2: Filter by availability

```bash
uv run python3 .claude/skills/esphome-fleet-update/availability.py <yml1> <yml2> ... --json
```

Reads `fleet/availability.yml`. Returns each YAML tagged `eligible` or `skipped` with a reason. The script defaults to today's date; pass `--date YYYY-MM-DD` only if testing.

Also call `inventory.py --json` and cross-reference: any YAML whose device has `Source = vendor` is bucketed as `skipped (vendor — separate channel)` regardless of what the review said.

### Step 3: Confirm scope

Show the user the eligible list and the skipped list in one block, ask for a single OK before any compile begins. This approves the *scope* and kicks off the compile pass — it does not authorize flashing. The actual flash is gated per-device in Step 6, after each device has compiled and been reachability-checked. Make that split explicit in the prompt so a single-device run doesn't read as asking twice for the same thing.

```
Eligible (4):
  bedroom-headboard.yml
  garage-door.yml
  panthella.yml
  workbench-light.yml

Skipped (3):
  outdoor-christmas-lights.yml — seasonal, next active November (in 157 days)
  space-heater.yml — seasonal, next active October (in 126 days)
  airgradient-one.yml — vendor (AirGradient firmware channel)

Compile these 4 eligible devices? (Each flash is confirmed separately afterward.) (y / n)
```

### Step 4: Compile pass

Compile every eligible device sequentially:

```bash
uv run esphome compile <yml>
```

- Success → mark ready-to-flash.
- **Failure → halt the entire run.** Print the error and the YAML that failed. A single compile failure usually indicates a shared `common/` problem that will break the rest too — better to surface it once than 11 times.

This is the natural stop-at-compile boundary called out by `feedback_plan_stops_at_compile.md`. The user has explicitly opted into going past it for this skill, but the compile pass remains a clean exit point — if all compiles succeed but the user wants to defer flashing, "stop here" is a reasonable response at Step 5/6.

### Step 5: Reachability check

For each compiled device, ping the static IP from its YAML:

```bash
grep -E "^\s*static_ip:" <yml> | head -1
ping -c 1 -W 1 <ip>
```

Unreachable → move from `ready-to-flash` to `skipped (unreachable)` with the IP. Don't waste 60 seconds on an OTA timeout for a device that's unplugged.

### Step 6: Flash pass — one at a time

Order the queue lowest-blast-radius first:

1. Quiet devices (sensors, lights that aren't being watched right now)
2. esp32/esp-idf before esp8266/arduino (faster recovery if a flash misbehaves)
3. Critical-path last (`garage-door.yml`, `bedroom-headboard.yml`, `mahtanar-heatpump.yml`)

For each device, present a confirm prompt:

```
[3/4] bedroom-headboard.yml → 192.168.20.x
  Currently: 2026.4.5 → target: 2026.5.1
  Flash now? (y / skip / abort)
```

- `y` → `uv run esphome run <yml>`
- `skip` → mark skipped (manual), move to next
- `abort` → stop the whole flash pass; the unflashed remainder goes into the report

After each successful flash, re-query HA (`inventory.py --json` or a narrower template) and confirm the device's new `sw_version` matches the target. **Mismatch → warn but continue** — HA's device registry can lag the device by a few seconds after OTA.

### Step 7: Final report

Write a markdown summary to `/tmp/fleet-update-<ISO-timestamp>.md`:

- **Flashed** — old version → new version, confirmed via HA re-query
- **Skipped (seasonal)** — list with their next-active month
- **Skipped (offline / broken / unreachable / vendor)** — with reason
- **Failed** — compile or flash error verbatim
- **Follow-up** — any seasonal device that becomes eligible within 60 days (read `days_until_active` from the availability JSON)

Print the path at the end so the user can read it.

## Critical files

- `fleet/availability.yml` — the manifest. Skill reads, never writes. User hand-edits to add new seasonal/offline/broken entries.
- `.claude/skills/esphome-fleet-update/availability.py` — partition script. Stable interface; the model orchestrates everything else via Bash.
- `.claude/skills/esphome-release-review/inventory.py` — reused for HA cross-reference (Source + Framework + matched YAML + post-flash sw_version).

## Gotchas

- **`uv run esphome ...`, always.** `feedback_uv.md`. Bare `esphome` will not have the pinned version.
- **One compile failure halts the pass.** Don't try the next one "in case it's local". The shared `common/` layer means most compile breakage is fleet-wide.
- **Vendor devices need separate channel.** `Source = vendor` rows from `inventory.py` get filtered out at Step 2 with a clear "AirGradient/Nabu Casa/etc. firmware channel" note. Never invoke `esphome run` on them.
- **HA `sw_version` lag.** A device that just flashed can report the old version for a few seconds. Warn-but-continue on mismatch; don't loop-retry.
- **Mahtanar is a fork.** If `mahtanar-heatpump.yml` is in scope, ensure the target ESPHome version has been tested upstream before flashing — the heat pump is the household's only HVAC. (See `project_mahtanar_hardware.md`, `project_mitsubishi_cn105.md`.) Prefer prompting the user explicitly before this one.
- **No rollback.** A bricked device needs hardware recovery (factory partition in 2026.5.0+, USB-UART before that). Out of scope here — the per-device confirmation is the safety mechanism.
- **`fleet/availability.yml` is hand-curated.** If a device looks "always offline" in HA, don't auto-mark it broken. Surface the observation and let the user add the entry.

## What NOT to do

- Don't batch flashes. Sequential with confirmation is the contract.
- Don't continue past a compile failure.
- Don't edit `fleet/availability.yml`. Suggest changes to the user, never write them.
- Don't edit any device YAML. This skill recompiles and flashes; YAML edits belong to the review + manual-edit cycle.
- Don't skip the reachability check. The 60-second OTA timeout per offline device is a sharp time cost on a 10+ device pass.
- Don't claim a flash "worked" without re-querying HA `sw_version`. The local `esphome run` returning 0 means the OTA finished, not that the device booted into it.

## Trigger phrasing

- "Flash the affected devices"
- "Update the fleet to 2026.5"
- "Run the fleet update"
- "OTA the devices from the review"
- "/esphome-fleet-update"
- "/esphome-fleet-update --review /tmp/release-review-2026.5.md"
- "/esphome-fleet-update --devices bedroom-headboard.yml,panthella.yml"
