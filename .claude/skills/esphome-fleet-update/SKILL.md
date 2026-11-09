---
name: esphome-fleet-update
description: Compile and OTA-flash every ESPHome device behind the pinned version with one approval, via `fleet_update.py`. Filters out seasonal/offline/broken devices via `fleet/availability.yml`, skips unreachable devices before compiling, flashes critical devices last with their own approval, turns switches back on through Home Assistant, and halts if a compile or flash fails or a device doesn't come back. Use when the user says "flash the affected devices", "update the fleet", "run the fleet update", "OTA the devices", "/esphome-fleet-update". Optional `--devices a.yml,b.yml` narrows the scope.
---

# ESPHome fleet update

A thin wrapper around `fleet_update.py`, which does the work. The user can also run `mise run fleet-update` directly, with no Claude session; there the script asks its own prompts.

Claude's Bash has no TTY, so the script can't prompt here. Claude collects the approvals instead:

1. **Plan.** Run `mise run fleet-update --plan` (add `--devices <yml,...>` if the user narrowed the scope) and show the table.
2. **Approve.** Get one approval for the whole run. Name any critical devices in the table as a separate decision (step 4).
3. **Run.** Run `mise run fleet-update --yes` with the same scope, using `run_in_background`, then wait for the completion notification. Unapproved critical devices aren't compiled or flashed; the report lists them as pending.
4. **Critical devices.** For each pending device, ask the user. If approved, run `mise run fleet-update --devices <yml> --approve-critical <yml> --yes`.
5. **Report.** Relay what the final report says: failures first, then anything not flashed, then flashed devices and the switches turned back on. The script prints the report's path (`/tmp/fleet-update-<timestamp>.md`).

If the user approves critical devices in step 2, pass them to the step 3 run with `--approve-critical` and skip step 4.

## Flags

| Flag | Effect |
|---|---|
| `--devices a.yml,b.yml` | Narrow the scope |
| `--plan` | Stop after printing the table |
| `--compile-only` | Stop after compiling, with no approval needed since no device is touched |
| `--yes` | Skip the Proceed prompt |
| `--approve-critical a.yml,b.yml` | Pre-approve critical devices |
| `--date YYYY-MM-DD` | Override the date used for availability, for testing |

The release review doesn't run automatically. If the user wants to know whether to update, run `esphome-release-review` first.

## When a run halts

A halt leaves the remaining devices unflashed and listed in the report. Don't re-run to get past it. Show the log tail and the report, and let the user decide. Re-running later is safe, since finished devices show as up to date.

## Gotchas

- **`uv run esphome ...`, always.** Bare `esphome` won't have the pinned version. The script handles this; it matters for any manual command.
- **Vendor devices update through a separate channel.** Vendor devices have no repo YAML, so the script never considers them. Never run `esphome run` on them.
- **Mahtanar is the household's only HVAC, and it's a fork.** Before approving `mahtanar-heatpump.yml`, make sure the target ESPHome version has been tested with the fork. See `project_mahtanar_hardware.md` and `project_mitsubishi_cn105.md`.
- **There's no rollback.** A bricked device needs hardware recovery: the factory partition on 2026.5.0+, USB-UART before that.
- **`fleet/availability.yml` is hand-curated.** If a device looks permanently offline, tell the user and let them add the entry. Never edit the manifest or a device YAML from this skill.
