# OpenGarage

The garage door opener at `opengarage.lan.kitcorey.com` is an [OpenGarage v2.2](https://opengarage.io) (ESP-12 / ESP8266, 4 MB flash, no on-board USB-serial chip). Originally flashed with OpenGarage's own firmware; being migrated to ESPHome — config in [`../garage-door.yml`](../garage-door.yml).

## Why ESPHome

OpenGarage's stock firmware works fine, but the HA side relies on a REST integration that polls `/jc` every few seconds. Switching to ESPHome gives us:

- Native API on TCP 6053 with HA auto-discovery (no HACS integration to maintain).
- Sensor and debounce behavior tunable in YAML alongside the rest of the fleet.
- The same OTA / logs / packages workflow as every other device in this repo.

The board is **pre-v2.3** (confirmed via the OG `/jc` endpoint: `has_swrx: 0`, `secv: 0`), so we don't need any Security+ component (no `ratgdo` / `esphome-secplus-gdo`). Door state comes purely from a ceiling-mounted HC-SR04 ultrasonic, and "press the button" is a 1-second momentary pulse on a relay wired into the opener's pushbutton input.

## Hardware

ESP-12 (ESP8266) on the OpenGarage v2.2 PCB. Pin assignments — see the [v2.2 schematic](https://github.com/OpenGarage/OpenGarage-Hardware/blob/master/Schematic/2.2/og22_schematic.png):

| GPIO  | Function                                                  |
|-------|-----------------------------------------------------------|
| 0     | Case-mounted pushbutton (also: ESP8266 boot mode)         |
| 2     | Status LED                                                |
| 4, 5  | Spare I²C (SDA/SCL — for AM2320 temp/humidity expansion)  |
| 12    | Ultrasonic trigger                                        |
| 13    | Buzzer (PWM-driven for tones)                             |
| 14    | Ultrasonic echo                                           |
| 15    | Relay (opener button input)                               |
| 16    | Hardware reset                                            |

**Critical gotcha:** in ESPHome, reference pins by raw GPIO number (`GPIO12`), not D-notation (`D5`, `D6`). The D-notation maps cause a bootloop on this hardware revision.

## Flashing

The v2.2 board has no USB-serial chip — the microUSB port supplies power only. **First flash requires a USB-UART adapter** (FTDI, CP2102N, etc.) on the 6-pin programming header inside the case. After ESPHome is on, all subsequent updates are OTA (configured in `common/s31_base.yml`).

### Programming header pinout

| Pin | Net           | USB-UART side      |
|-----|---------------|--------------------|
| 1   | ETX (ESP TX)  | RX                 |
| 2   | VIN (**5V**)  | VCC (5V)           |
| 3   | GPIO0         | (use case button)  |
| 4   | ERX (ESP RX)  | TX                 |
| 5   | GND           | GND                |
| 6   | RST           | (unused)           |

- **VIN expects 5V**, not 3.3V — it feeds through diode D1 into the on-board regulator. Set the adapter to 5V power with 3.3V logic. Wrong voltage will fry the regulator.
- The case button is wired to GPIO0, so it doubles as the ESP8266 boot button — no separate jumper wire needed to pull GPIO0 low. Hold it while plugging in the adapter, release after ~1 s.
- CP2102N on macOS is reliable up to 230400 baud; higher speeds (e.g. 460800) produce "Invalid head of packet" mid-transfer. FTDI handles 460800 fine.

### Backing up the stock firmware (one-time)

Before flashing ESPHome, dump the original 4 MB image so a rollback is possible. Wire the adapter per the table above and enter bootloader (hold case button → plug in USB → release after ~1 s), then:

```sh
uv run esptool --port /dev/cu.usbserial-XXX --baud 230400 read-flash 0x0 0x400000 og-backup-1.bin
# power-cycle into bootloader again
uv run esptool --port /dev/cu.usbserial-XXX --baud 230400 read-flash 0x0 0x400000 og-backup-2.bin
shasum -a 256 og-backup-*.bin
```

Two reads with matching SHA-256 = known-good image. Optionally `strings og-backup-1.bin | grep <ssid>` to confirm WiFi creds are captured.

**Do not commit the binary** — it contains WiFi password, OG cloud token, and any MQTT/IFTTT creds from `/jc`. Store the deduped, dated file (e.g. `opengarage-og124-stock-2026-05-18.bin`) in 1Password as a document attachment alongside a note recording: chip (ESP8266EX, 4 MB, 26 MHz crystal), MAC, OG firmware version, and date.

To restore later: same wiring, same bootloader entry, then `uv run esptool --port /dev/cu.usbserial-XXX --baud 230400 write-flash 0x0 <backup>.bin`. Whole-flash restore brings the user-data region back too, so the device rejoins WiFi and the OpenGarage HACS integration repolls `/jc` without reconfiguration.

### Procedure

1. `uv run esphome compile garage-door.yml` to validate.
2. Snapshot the existing OG firmware config from `http://opengarage.lan.kitcorey.com` — backups of `/jc` and `/jo` are already saved at the repo root as `opengarage-jc-backup.json` / `opengarage-jo-backup.json`. If you also want a full firmware rollback path, do the binary backup above first.
3. Unplug microUSB, open the case, wire the USB-UART adapter per the table (remember to cross TX/RX).
4. **Hold the case button** while plugging the adapter's USB end into the laptop, then release after ~1 second. The ESP is now in bootloader mode.
5. `uv run esphome run garage-door.yml --device /dev/ttyUSBn` (Linux) or `/dev/cu.usbserial-XXX` (macOS).
6. After upload finishes, unplug the adapter, plug microUSB back in to power-cycle, then `uv run esphome logs garage-door.yml` to confirm the device joined the network.
7. Reassemble.

If `esptool` times out with "Failed to connect", power-cycle and retry — the bootloader window is finicky and there's no DTR/RTS auto-reset on this board.

## Post-flash housekeeping

- **Remove the OpenGarage HACS integration** from Home Assistant. Otherwise it'll keep polling the now-dead `/jc` endpoint and spam errors. The ESPHome native API auto-discovers the new device.
- Static IP `192.168.20.14` is preserved in the YAML, so DNS / `opengarage.lan.kitcorey.com` keeps resolving and any HA automations referencing that hostname keep working.
- The config includes `common/garage_wifi_roam.yml`, so the device joins the rest of the garage fleet's roam package on the nanoHD AP and drops off the `kick-client.py` fallback list.
- After auto-discovery, two new `number.*` config entities show up alongside the cover (`door_threshold_cm`, `relay_click_time_ms`) — initial values match the OG defaults so no immediate tuning is needed. The cover entity now reports `current_operation` (OPENING/CLOSING/IDLE).

## Sensor design

The HC-SR04 can produce spurious echoes, and a low object or person partly in the sensor cone makes it much worse — raw readings scatter chaotically (a few cm up to ~3.9 m) and, worse, the beam scatters so badly that **70–90 % of pings time out entirely** (no echo at all). The state derivation has to survive both the scatter and the sparsity.

**Why an upper quantile, not a median.** The classifier asks the wrong question if it asks "what's the typical distance right now," because clutter under the sensor drags the typical value down. The key insight is asymmetric: an object low in the cone can only *add* spurious **near** readings — it cannot manufacture a **far** one. The floor (~2.6 m) stays intermittently visible past a partial obstruction. A real *open* drops the door panel under the sensor and physically blocks the floor (no far return is possible), and a parked car roof (~1 m) blocks it the same way. So the right question is "**is the floor still reachable past the clutter?**" — answered by an upper quantile of recent readings. Clutter sits in the bottom of the distribution and is ignored; only a *sustained* loss of far returns moves state.

A median is exactly wrong here: with `window_size: 15` only 8 of 15 near readings dipped the published value below the 0.50 m threshold, so an object under the sensor flapped `door_closed` open/closed every 10–40 s (confirmed live 2026-05-28 — distance bounced wildly while the object was present and locked back to a clean steady ~2.6 m the instant it was removed; the physical door never moved).

**The quantile alone wasn't enough — ping rate was the real lever.** Swapping median → quantile at the original `update_interval: 500ms` only cut the flapping to ~1/min. The reason showed up in a raw capture: with the object present ~92 % of pings timed out, leaving only **~1.7 valid samples per 10.5 s window** — far too little for any percentile to be stable, and the longest stretch with *no* floor sighting at all ran 55–68 s. Dropping to **`update_interval: 100ms`** was the fix: ~3× as many pings packs ~15+ valid readings into each window and shrinks the worst echo-loss gap to ~8 s. With that change the device held `door_closed` **closed for ~46 min straight** with the object in place (live 2026-05-28). (The HC-SR04 read cycle floors around ~170 ms on this ESP8266, so `100ms` self-limits to ~6 reads/s — that's fine. Stay ≥ 60 ms: the sensor's minimum inter-ping spacing.)

The config uses two stacked layers:

1. **Sample-level quantile filter** (`window_size: 60`, `quantile: 0.8`) on the raw distance — ~6 s of wall-clock at the nominal 100 ms ping (~10 s at the ~170 ms actually achieved). NaN timeouts are dropped before the percentile is taken, so the published value is ~the 80th percentile of the window's *valid* readings: ~80 % of them must be near before it reports near. As long as the floor shows up in ≳20 % of valid samples, the value stays far and `door_closed` holds. A real open (floor fully blocked) and a car roof still register.
2. **State-level debounce** (`delayed_on_off: 10s`) on the `door_closed` template binary sensor. A change has to hold for 10 seconds before HA sees it. This **must** be the single `delayed_on_off` filter, *not* stacked `delayed_on` + `delayed_off`: chaining them passes a falling edge through immediately but holds the rising edge for the full delay, so a brief sub-threshold spike in the quantile output schedules an OFF that the (still-delayed) recovery can't cancel — manufacturing a ~10 s "open" pulse from a sub-2 s dip. This was the cause of a flapping regression diagnosed live 2026-05-28 (a 1.5 s and a 0.13 s dip each produced an exactly-10 s-delayed OFF in HA). `delayed_on_off` re-arms a single timer on each edge, so a dip that recovers within the delay cancels its own pending OFF.

The two layers stack: clutter that occupies the bottom of the distribution can't move state, and a genuine transition has to survive both the quantile window and the debounce. A real door *open* registers in HA within ~20–25 s (quantile window + debounce).

A `skip_initial: 60` filter sits after the quantile as a **cold-boot guard**: on boot the window starts empty, so the first reading or two (easily a stray near return if something's under the sensor) would otherwise publish a bogus low distance and flap `door_closed` to "open" for a few seconds. Suppressing publishes until the window fills keeps `door_distance.state` NaN — so `door_closed` reports *unavailable* rather than a false "open" — for the ~10 s warmup after each reboot/OTA. The state-level `delayed_on_off` debounce can't catch this case because it only gates *changes*, not the binary sensor's very first published state.

The quantile is a **compile-time substitution** (`distance_quantile`, default `'0.8'`), *not* an HA `number` slider — the `quantile:` filter argument is a config constant baked in at compile time, so it can't be runtime-templatable. Tune it by edit + recompile. If false positives reappear, the tuning ladder (see the inline comments in `garage-door.yml`) is: (1) raise the quantile toward 0.9 to tolerate more spurious-far noise, or lower toward 0.7 for faster real-open detection (0.5 is equivalent to the old median); (2) widen `window_size`; (3) lengthen `delayed_on`/`delayed_off`. A fourth, blunter lever if the obstruction problem ever returns worse: ping faster still (but never below the sensor's 60 ms floor).

**Residual limitation.** An object or person standing perfectly still *directly* under the sensor — close enough that every return reads < 0.50 m for longer than the debounce — fully occludes the floor, and ultrasonic alone cannot distinguish that from a closed panel. The quantile + fast pinging fixes objects to the side and partial/passing people (which still let the floor peek through), but not sustained dead-centre full occlusion. The durable fix, if it ever bites in practice, is a physical rail reed or tilt switch on the door itself — out of scope for the software-only filter.

The door threshold and the relay click time are exposed as HA `number` entities (`door_threshold_cm` default 50, `relay_click_time_ms` default 1000) — persisted to flash via `restore_from_flash`, so tuning is a slider in HA, not a re-flash. A `delta: 0.01` filter on the ultrasonic sensor suppresses idle chatter to HA; the 1 cm dead-zone is far below the 50 cm threshold so it doesn't affect state evaluation in practice.

Close commands fire a **~5-second warning beep** before pulsing the relay (`rtttl` driving the GPIO13 buzzer). This replicates OG's `alm=1`/`aoo=1` UL325-style safety behavior — anyone in the bay gets audible warning before the door starts moving. Open and stop fire immediately, since opening can't pinch. The cover publishes `current_operation` (OPENING/CLOSING/IDLE) around each action so HA shows the in-flight state.

## References

- [OpenGarage hardware repo](https://github.com/OpenGarage/OpenGarage-Hardware) — schematics for every revision (1.0, 1.1, 1.4, 2.0, 2.2, 2.3+).
- [OpenGarage firmware repo](https://github.com/OpenGarage/OpenGarage-Firmware) — the stock firmware we're migrating away from; useful for cross-referencing pin assignments, default thresholds (`dth=50`, `vth=150`, `dri=500`, `cmr=10`, etc.), and `/jc` JSON field meanings.
- [OpenGarage user manual](https://opengarage.github.io/OpenGarage-Firmware/1.2.4/manual/) — describes the original behavior we're matching.
- [gabe565/esphome-configs (opengarage/)](https://github.com/gabe565/esphome-configs/tree/main/opengarage) — another OG ESPHome config for the same hardware; source of several patterns in `garage-door.yml` (HA-tunable `number` thresholds, `status_led`, `current_operation` transitions, `delta` filter, self-pulsing relay, `play_rtttl` service). See the in-file header for borrowed-vs-diverged notes.
