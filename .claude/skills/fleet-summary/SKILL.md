---
name: fleet-summary
description: One-shot, deterministic snapshot of the ESPHome fleet's current state — every device's online/offline liveness, installed ESPHome version, matched repo YAML, Source (repo/vendor), and whether its liveness matches expectations (a seasonal device down in the off-season is fine; an active device offline is a flag). Read-only; no flashing, no YAML edits, no manifest writes. Use when the user asks "fleet summary", "what's my fleet doing", "are all my devices online", "is anything offline", "give me a fleet snapshot", "/fleet-summary".
---

# Fleet summary

A read-only "what is my fleet doing right now" snapshot. Sibling to `esphome-release-review` (upstream changelog comparison) and `esphome-fleet-update` (compile + OTA). This skill answers neither "what changed upstream" nor "flash it" — only the present-tense status, with **liveness** (online/offline) as the headline, which neither sibling reports.

The whole report is computed by one script — `summary.py` — in a single HA round-trip. The model's job is to run it, lead with the flags, narrate briefly, and offer follow-ups. The skill never edits YAML, never writes `fleet/availability.yml`, and never flashes.

## Prerequisites

The script reaches HA via REST (`/api/template`) and reads this repo's YAMLs + `fleet/availability.yml`. Set in the shell before running:

- `HA_URL` — e.g. `http://homeassistant.local:8123` (or a Tailscale/Cloudflared host)
- `HA_TOKEN` — long-lived access token from `<HA>/profile/security`
- `HA_INSECURE_TLS=1` — only if HA is behind a self-signed cert

If `HA_URL`/`HA_TOKEN` are unset the script exits 2 with a clear message — relay it and tell the user how to set them; do not substitute another data source.

## Run

```bash
uv run python3 .claude/skills/fleet-summary/summary.py            # markdown table
uv run python3 .claude/skills/fleet-summary/summary.py --json     # structured, to iterate
uv run python3 .claude/skills/fleet-summary/summary.py --date 2026-12-15   # test seasonal expectation
```

`uv run` because the imported `availability.py` needs PyYAML. The repo path is derived from the script's location, so it works from any clone; the user's pwd should still be `/home/kit/repos/esphome`.

Columns: `Device | State | Online | ESPHome | Source | YAML | Note`, then a rollup line (`N devices: X online, Y down (Z expected), W flags`), a within-fleet drift line, and a list of repo configs that matched no live device.

### What the script automates

It is a thin orchestrator that **imports** the two sibling scripts rather than reimplementing them:

- **Liveness + device registry** — one `/api/template` call. `online` = any of the device's entities is not `unavailable`/`unknown`.
- **Version parse, repo-YAML match, Source (repo/vendor)** — reused verbatim from `inventory.py`.
- **Seasonal / offline / broken expectation** — reused verbatim from `availability.py` (date-driven; thread `--date` through to test).
- **State** — the value-add: liveness × expectation discrepancy:
  - online + (active / seasonal-in-season) → `ok`
  - offline + (seasonal-out / offline / broken) → `expected down` (with reason)
  - offline + (active / in-season) → `⚠ unexpectedly offline`
  - online + manifest says offline/broken → `⚠ manifest stale` (marked X, but online)
  - online + seasonal-out-of-season → `on (out of season)` (informational, not a flag)
  - `Source = vendor` → `vendor (separate channel)` (liveness still shown)
  - no matched YAML → `unmanaged (no repo YAML)`
- **Drift** — highest ESPHome version among `Source = repo` devices and which lag it.
- **Reverse map** — repo YAMLs with `substitutions:` that match no live device (e.g. `s31-spare`).

## Presenting

Keep it tight (~150–250 words). Lead with the ⚠ flags, not the table:

1. **Flags first.** Any `⚠ unexpectedly offline` or `⚠ manifest stale` rows — name them and what they mean. If `flags` is 0, say so in one line ("everything matches expectations").
2. **Then the table**, verbatim from the script.
3. **Rollup + drift** in a sentence — "15/18 online, 3 expected-down (seasonal); fleet split across 2026.4.5 and 2026.5.1." Don't re-list every laggard the drift line names; summarize.
4. **Configs with no live device** — mention briefly (likely a spare or a renamed/disabled device).

## Gotchas

- **Title↔YAML matching is heuristic.** The script matches `device.name` / `name_by_user` against each YAML's `substitutions.name` and `friendly_name`, disambiguating duplicates by chip family. A `no YAML matched` row usually means the HA name diverged from both — not a missing config.
- **Disabled devices are invisible.** `integration_entities('esphome')` only returns enabled devices, so a disabled one won't appear. If the user asks why a known device is missing, that's usually why.
- **Vendor rows aren't actionable here.** `Source = vendor` (AirGradient, HA Voice PE, etc.) updates through a separate channel; liveness is shown but this skill can't flash them.
- **`fleet/availability.yml` is hand-curated — never auto-edit it.** A `⚠ manifest stale` row (marked offline/broken but actually online), or a seasonal device that looks permanently offline in season, is a *suggestion* to surface to the user. Propose the manifest change; let them make it.
- **Drift is within-fleet only.** The "highest" version is just the newest among the user's own devices — it says nothing about the latest *upstream* ESPHome release. For that, point at `/esphome-release-review`.
- **`sw_version` can lag.** Right after an OTA, HA's device registry may report the old version (or a transient `unavailable`) for a few seconds.

## Follow-ups

This skill is read-only. After presenting, offer the write-side siblings as appropriate:

- **`/esphome-release-review`** — compare the fleet against an upstream release / changelog (the upstream-version question this skill deliberately doesn't answer).
- **`/esphome-fleet-update`** — recompile and OTA-flash devices, with seasonal/availability filtering and per-device confirmation.

Don't fetch changelogs, edit YAML, or write the manifest from this skill — those belong to the siblings or a manual edit.

## Trigger phrasing

- "Fleet summary" / "give me a fleet summary"
- "What's my fleet doing?"
- "Are all my devices online?" / "Is anything offline?"
- "ESPHome fleet snapshot" / "fleet status"
- "/fleet-summary"
- "/fleet-summary --date 2026-12-15"
