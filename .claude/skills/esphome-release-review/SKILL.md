---
name: esphome-release-review
description: Personalized ESPHome fleet release review. Use when the user asks to review an ESPHome release, check breaking changes for their devices, or decide whether to recompile/reflash ("any breaking esphome changes?", "review my esphome fleet", "should I update esphome", "/esphome-release-review [version|URL]"). Sweeps every configured ESPHome device via Home Assistant (REST `/api/template`, no `.storage` access required), compares each device's flashed `sw_version` against the target release, cross-references breaking changes against this repo's YAMLs (including `common/*.yml` includes), and produces a fleet-status table plus ranked YAML-side improvements with file:line pointers. Flags vendor-managed firmware (AirGradient, HA Voice PE, etc.) so the user knows which devices update through a separate channel.
---

# ESPHome release review

Produce a personalized review of an ESPHome release for the user's fleet. They already know what's in the changelog; they want to know what *applies* to their devices and which YAMLs to touch.

## Inputs

- **URL** — e.g. `https://esphome.io/changelog/2026.5.0.html` or a GitHub release URL. Use as-is.
- **Version** — e.g. `2026.5` or `2026.5.0` → construct `https://esphome.io/changelog/<MAJOR>.<MINOR>.0.html` (patch releases append to the same page).
- **No arg** — fleet sweep against latest stable. Discover stable from `https://api.github.com/repos/esphome/esphome/releases/latest` (`tag_name`). If that fails, fall back to scraping the changelog index at `https://esphome.io/changelog/`. If both fail, **stop and tell the user** — do not invent a version.

## Prerequisites — HA credentials

The inventory script reaches HA via REST. Set in the shell before running the skill:

- `HA_URL` — e.g. `http://homeassistant.local:8123` (or a Tailscale/Cloudflared host)
- `HA_TOKEN` — long-lived access token from `<HA>/profile/security`
- `HA_INSECURE_TLS=1` — only if HA is behind a self-signed cert

If they aren't set, `inventory.py` exits 2 with a clear error. Tell the user how to set them; do not try to substitute another data source.

## Methodology — five steps

### Step 1: Fleet inventory

Run the sibling script. It hits HA's `/api/template` to render a Jinja query over the device registry — no filesystem access to `.storage` needed, so it works from any clone of this repo.

```bash
python3 .claude/skills/esphome-release-review/inventory.py
```

Add `--json` if you want to filter/iterate programmatically. The default markdown table is what goes into the report.

Output columns: `Device | ESPHome | Source | Chip | Framework | YAML | Notes`.

- **Source = `repo`** — the device's firmware was built from a YAML in this repo. Recompile path: edit YAML → `esphome run`.
- **Source = `vendor`** — `sw_version` matches the `(ESPHome X.Y.Z)` project-metadata pattern *and* no YAML matched. Vendor controls the build; recompiling locally won't reach this device.
- **Notes "project version X.Y.Z"** — the firmware emits an `esphome.project.version`. With a YAML matched, this is just user-set metadata (e.g. mahtanar's `mitsubishi-huzzah32.yaml` reports `1.4 (ESPHome 2026.3.3)` because the package sets `project.version: '1.4'` — still user-owned).

### Step 2: Determine target version + range

- **Target** = arg if provided, else GitHub `releases/latest` `tag_name`.
- **Per-device range** = `(esphome_version, target]`.
- **Union of minors** across the fleet → the set of changelog pages to fetch (typically 3–6 per release for a fleet that's drifted across a few minors).

### Step 3: Fetch release notes

Use `WebFetch` on `https://esphome.io/changelog/<MAJOR>.<MINOR>.0.html` for each minor. The page covers all patch releases for that minor.

Extraction prompt (verbatim):

> Extract from this ESPHome changelog page:
> 1. **Breaking changes** with full detail. Include the verbatim component name (e.g. `sensor.bme280`), platform name, and any config keys that were renamed/removed. Note framework scope if stated (esp-idf, arduino, esp8266, rp2040, esp32-s3).
> 2. **Deprecations** with removal-version deadline if stated.
> 3. **New components and platforms** — name + one-line purpose.
> 4. **Notable improvements to existing components** — group by component name.
> 5. **Core/build-system changes** that affect compile or OTA.
> Don't summarize. I need component names, platform names, and config keys verbatim so I can grep YAML files.

Fallback if `esphome.io/changelog/<X.Y>.0.html` 404s: `https://github.com/esphome/esphome/releases/tag/<version>`.

### Step 4: Cross-reference

For each **breaking change**:

1. **Framework filter.** If the change is scoped to esp-idf or arduino, intersect with the per-device `Framework` column. ESP8266-only changes apply only where `Chip = esp8266`.
2. **Vendor filter.** Skip YAML grep for `Source = vendor` rows; track them only as "vendor channel is N ESPHome releases behind."
3. **YAML grep.** `grep -rn` the verbatim component / platform / config-key across `*.y*ml` and `common/*.yml`. **A hit in `common/` cascades** — one edit fans out to every YAML that `!include`s that common file. Surface that as a single edit, not N.
4. **Verdict per change**: **No** (no hit), **Watch** (tangential or gradual), or **Yes** (direct usage — cite `file:line`).

For each **notable improvement**:

- Same grep + framework filter.
- **Per-device range filter.** An improvement landed in 2026.X applies only to devices currently below 2026.X — devices already at or past it have already received it on their last flash. When listing affected YAMLs for a 2026.3 light optimization, exclude `bedroom-headboard.yml` if the headboard is already on 2026.3.x or newer. This is the single most common error in this skill — always intersect the improvement's release minor with each device's current `ESPHome` column from Step 1.
- Rank **High** (clear win, file:line cited), **Medium** (worth considering, no obvious action), **Skip** (component not used).

### Step 5: Render the report

**Linking rule.** Every named feature, config key, component, or breaking change cited in the report should be a markdown link the user can click. Pick the most specific target available, in this order of preference:

1. **GitHub PR** — `https://github.com/esphome/esphome/pull/<N>` when the changelog entry shows a `(#<N>)` reference. This is the most informative target.
2. **Changelog section anchor** — `https://esphome.io/changelog/<X.Y>.0.html#<slug>` if you know the anchor exists. Don't guess slugs; only use if you have it from the page.
3. **Changelog page** — `https://esphome.io/changelog/<X.Y>.0.html` as fallback.
4. **Component docs** — `https://esphome.io/components/<component>.html` for component-name citations when no PR/changelog anchor fits.

Inline the link on the *first* mention of each feature in the report. Subsequent mentions can be plain text.

```markdown
# ESPHome <target> — Fleet review

## Fleet status (<N> devices)

<inventory.py markdown table>

## Breaking changes — <one-line verdict>

| Change | Framework | Affected | Action |
|---|---|---|---|
| [`foo` → `bar` rename](https://github.com/esphome/esphome/pull/12345) | esp-idf | bedroom-headboard.yml, ble_tracker.yml | rename `foo:` → `bar:` (file:line) |

## Worthwhile improvements

**High value (do these)**
1. **[<feature>](<link>)** — <one paragraph: why, file:line, complementary or replacement>

**Medium value (consider)**
- ...

**Skip / don't apply**
- <component not used / fork unaffected / etc.>

## Vendor-managed firmware (separate channel)

- AG One / AG Open Air — AirGradient firmware update channel
- HA Voice PE — Nabu Casa OTA
- <other vendor-managed devices>

## Suggested order of work

1. <common/ edit if any — fixes N devices at once>
2. <per-device YAML changes>
3. <recompile + OTA order, lowest-risk first>
4. <vendor-managed items, separate channel>

> Once the YAML edits are in, run `/esphome-fleet-update` to recompile and OTA-flash the affected devices with seasonal/availability filtering and per-device confirmation.
```

## Gotchas

- **Two `sw_version` shapes.** Pure: `2026.4.3 (2026-05-08 03:25:18 +0000)`. Project: `5.3.3 (ESPHome 2025.12.4)`. The "project" shape can mean *either* vendor firmware OR user-owned firmware that sets `esphome.project.version` (e.g. mahtanar). The script's `Source` column already handles this — trust it, don't re-derive from `sw_version_raw`.
- **`pyproject.toml` is a minimum, not a pin.** `esphome>=2025.9.1` lets `uv sync` pull whatever's latest. Flashed `sw_version` is ground truth, not the pin.
- **Framework breaking changes are framework-scoped.** Check the `Framework` column before flagging an esp-idf-only breakage on an arduino device.
- **`common/` edits cascade.** A grep hit in `common/wifi.yml` affects every device that includes it — surface as one fix, not N.
- **Disabled config entries are filtered out.** `integration_entities('esphome')` only returns enabled devices, so disabled ones don't appear in the inventory and won't be reviewed. That's usually correct (no review needed for a disabled device); call it out if the user asks why a known device is missing.
- **Vendor devices need separate guidance.** Don't recommend YAML edits for `Source = vendor` — those changes have no effect on the device. Point at the vendor's update channel instead (AirGradient, Nabu Casa, etc.).
- **mahtanar is a fork.** If the heat pump shows `Source = repo` and the user wants to upgrade ESPHome, check whether `tinwer-group/mahtanar` has been tested on the target version before recommending the recompile. (See project memory: `project_heatpump_setup.md`.)
- **Title-to-YAML matching is heuristic.** The script tries `device.name` then `name_by_user` then YAML `friendly_name`, and disambiguates duplicates by chip family. If a row shows `no YAML matched` and you know it should match, the device's HA name probably diverged from both the YAML's `substitutions.name` *and* `friendly_name` — fix by adjusting one of them so they agree.
- **No file:line, no recommendation.** "Update your YAML" is unhelpful; `bedroom-headboard.yml:42 uses the deprecated key` is actionable.

## What NOT to do

- Don't dump every changelog bullet — filter to what applies. A quiet release with zero applicable changes gets a one-line "nothing applies."
- Don't bury the verdict. Each breaking change leads with No / Watch / Yes.
- Don't recommend code changes as part of the review. The skill produces a report. Acting is a follow-up turn.
- Don't fabricate. If the changelog page is ambiguous or 404s, say so.

## Output budget

~500 words for a typical sweep across 3–4 minors. Expand only when breaking changes pile up or a `common/` edit cascades. A clean release with nothing applicable gets ~150 words.

## Trigger phrasing

- "Review my esphome fleet"
- "Any breaking esphome changes?"
- "Should I update esphome?"
- "What's in esphome 2026.5?"
- "/esphome-release-review"
- "/esphome-release-review 2026.5"
- "/esphome-release-review https://esphome.io/changelog/2026.5.0.html"

If unclear which release the user means, ask once (current stable, RC, specific version) — don't guess.
