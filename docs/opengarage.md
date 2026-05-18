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

The v2.2 board has no USB-serial chip — the microUSB port supplies power only. **First flash requires an FTDI adapter** on the 6-pin programming header inside the case. After ESPHome is on, all subsequent updates are OTA (configured in `common/s31_base.yml`).

### Programming header pinout

| Pin | Net           | FTDI side          |
|-----|---------------|--------------------|
| 1   | ETX (ESP TX)  | RX                 |
| 2   | VIN (**5V**)  | VCC (5V)           |
| 3   | GPIO0         | (use case button)  |
| 4   | ERX (ESP RX)  | TX                 |
| 5   | GND           | GND                |
| 6   | RST           | (unused)           |

- **VIN expects 5V**, not 3.3V — it feeds through diode D1 into the on-board regulator. Set the FTDI jumper to 5V power with 3.3V logic. Wrong voltage will fry the regulator.
- The case button is wired to GPIO0, so it doubles as the ESP8266 boot button — no separate jumper wire needed to pull GPIO0 low.

### Procedure

1. `uv run esphome compile garage-door.yml` to validate.
2. Snapshot the existing OG firmware config from `http://opengarage.lan.kitcorey.com` — backups of `/jc` and `/jo` are already saved at the repo root as `opengarage-jc-backup.json` / `opengarage-jo-backup.json`.
3. Unplug microUSB, open the case, wire FTDI per the table (remember to cross TX/RX).
4. **Hold the case button** while plugging the FTDI's USB end into the laptop, then release after ~1 second. The ESP is now in bootloader mode.
5. `uv run esphome run garage-door.yml --device /dev/ttyUSBn` (use `ls /dev/ttyUSB*` to find the port).
6. After upload finishes, unplug FTDI, plug microUSB back in to power-cycle, then `uv run esphome logs garage-door.yml` to confirm the device joined the network.
7. Reassemble.

If `esptool` times out with "Failed to connect", power-cycle and retry — the bootloader window is finicky and there's no DTR/RTS auto-reset on this board.

## Post-flash housekeeping

- **Remove the OpenGarage HACS integration** from Home Assistant. Otherwise it'll keep polling the now-dead `/jc` endpoint and spam errors. The ESPHome native API auto-discovers the new device.
- Static IP `192.168.20.14` is preserved in the YAML, so DNS / `opengarage.lan.kitcorey.com` keeps resolving and any HA automations referencing that hostname keep working.
- The config includes `common/garage_wifi_roam.yml`, so the device joins the rest of the garage fleet's roam package on the nanoHD AP and drops off the `kick-client.py` fallback list.

## Sensor design

The HC-SR04 can produce spurious echoes — OpenGarage's firmware suppresses them with a 10-reading majority vote (`sfi=1`, `cmr=10`). The ESPHome config replicates this with two stacked layers:

1. **Sample-level median filter** (`window_size: 7`) on the raw distance reading. ~3.5 s window, immune to up to 3 outliers per window.
2. **State-level debounce** (`delayed_on: 5s` / `delayed_off: 5s`) on the `door_closed` and `vehicle_present` template binary sensors. A flap has to sustain for 5 seconds before HA sees a state change.

The two layers stack: no single bad reading can move state, and a burst of bad readings has to survive both the median window and the debounce. See the inline comments in `garage-door.yml` for the tuning ladder if false positives reappear (widen the median window first, then lengthen the debounce).

Close commands fire a **~5-second warning beep** before pulsing the relay (`rtttl` driving the GPIO13 buzzer). This replicates OG's `alm=1`/`aoo=1` UL325-style safety behavior — anyone in the bay gets audible warning before the door starts moving. Open and stop fire immediately, since opening can't pinch.

## References

- [OpenGarage hardware repo](https://github.com/OpenGarage/OpenGarage-Hardware) — schematics for every revision (1.0, 1.1, 1.4, 2.0, 2.2, 2.3+).
- [OpenGarage firmware repo](https://github.com/OpenGarage/OpenGarage-Firmware) — the stock firmware we're migrating away from; useful for cross-referencing pin assignments, default thresholds (`dth=50`, `vth=150`, `dri=500`, `cmr=10`, etc.), and `/jc` JSON field meanings.
- [OpenGarage user manual](https://opengarage.github.io/OpenGarage-Firmware/1.2.4/manual/) — describes the original behavior we're matching.
