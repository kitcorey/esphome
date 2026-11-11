# Move the E1004 to ESPHome with server-side dithering

> **Status (2026-11-10):** The frame runs ESPHome (`photoframe.yml`), flashed over USB on
> 2026-11-10, adopted in HA, and network OTA verified. It pulls from the upstream
> `esp32-photoframe-server` (`~/docker/photoframe-server`) at
> `http://photoframe-server.lan.kitcorey.com:9607/image`. See [Handoff](#handoff-2026-11-09) at
> the end. The research below that is the original November 2026 snapshot.

**Choose ESPHome**, and move the image processing you already like into a small Docker service on the home server that uses the same MIT-licensed `@aitjcize/epaper-image-convert` engine as esp32-photoframe. The frame then only has to wake, download a 1200x1600 PNG that is already dithered to the six inks, paint it, and go back to sleep. Updates become ordinary `esphome run` OTA pushes from your CLI over the LAN, with no GitHub release feed and no USB. This setup meets every requirement you listed. ESPHome has had a dedicated E1004 (T133A01) driver since **2026.7.0**. An ESPHome maintainer has published a working E1004 photo-frame config that uses exactly this "dithered PNG from a server, then deep sleep" pattern. And esp32-photoframe's own `process-cli --serve` already writes pure-RGB six-colour PNGs that ESPHome's colour mapper reproduces pixel-for-pixel. Second place goes to a **patched fork of esp32-photoframe** (configurable or LAN-upload OTA, plus fixes for the button and for rotation while the web UI is open). It keeps today's on-device features and SD-card independence, but you would be carrying a fork of a project that releases monthly. **Tesserae** comes third: it is technically impressive, but its OTA only accepts images signed with that project's key, which is the kind of project lock-in you want to avoid. TRMNL (monochrome on this panel), SenseCraft HMI (cloud-only) and a from-scratch Arduino build drop out. Every option needs one USB session to leave the current firmware, because its OTA cannot be redirected. Take the factory flash backup during that session. After that, routine updates are network-only.

## Five candidates fail on OTA control, colour, or the no-internet VLAN

The table ranks the options against your four constraints: ESPHome preferred, LAN OTA not tied to one project's releases, no internet on the VLAN, and photo-quality colour.

| Rank | Option | Colour photos on E1004 | Routine OTA | Works with no internet | Main cost |
|---|---|---|---|---|---|
| 1 | **ESPHome + dithering container** | Yes, pixel-exact from a pre-dithered PNG | Native ESPHome push from the CLI | Yes | Build and maintain a small render service |
| 2 | Patched esp32-photoframe fork | Yes, on-device FS dithering | Your own fork's releases, or a LAN upload endpoint | Yes, after the patch | About 60 lines of C, then a fork to rebase every month |
| 3 | Tesserae | Yes, server-side FS against a measured palette | Server-delivered, signed with the project's Ed25519 key | Frame yes; the server fetches firmware online | Project-key lock-in, AGPL, about 5 months old |
| 4 | OpenDisplay | Probably; panel driver exists | Through an HA update entity over Bluetooth | Unclear | Needs BLE, E1004 build status unverified |
| 5 | TRMNL + BYOS | **No**: monochrome on the E1004 | Self-hosted `firmware_url` | Yes | Not a colour photo frame |
| — | SenseCraft HMI (stock) | Yes | Seeed cloud and tools | **No** | Cloud platform |
| — | DIY Arduino (Seeed_GFX) | Yes | Anything you write | Yes | You write the whole app |

**Tesserae is the strongest non-ESPHome candidate.** It is a self-hosted Docker or HA-add-on server that does "Floyd-Steinberg dithering against the panel's **measured** color palette". Its device firmware lists the E1004 (`spectra6_t133a01_dual`) as "verified end-to-end on real hardware" ([Tesserae](https://github.com/dmellok/tesserae), [tesserae-device-firmware](https://github.com/dmellok/tesserae-device-firmware)). Its OTA is local in the sense that it arrives through your server. But the firmware checks each image against "its baked-in Ed25519 public key", and the signing host belongs to the project ([OTA contract](https://github.com/dmellok/tesserae/blob/main/docs/ota/contract.md)). You could not push your own build without re-keying and rebuilding. The project is also about five months old and ships server releases almost daily, which means a lot of churn. Nobody has checked its photo quality against esp32-photoframe's, and its pipeline sends photos through headless Chromium before dithering.

**TRMNL drops out on colour.** Seeed's own wiki says that on the E1004 "TRMNL firmware likewise renders its content in monochrome mode" ([Seeed wiki](https://wiki.seeedstudio.com/reterminal_e10xx_trmnl/)). An open issue also reports the E1004 Wi-Fi setup screen showing noise on recent builds ([#781](https://github.com/usetrmnl/trmnl-firmware/issues/781)). The BYOS OTA model (the server returns a `firmware_url`) would otherwise suit you well ([Terminus display/show.rb](https://github.com/usetrmnl/terminus/blob/main/app/actions/api/display/show.rb)). **SenseCraft HMI** puts albums and firmware updates behind Seeed's cloud tools, and no offline mode is documented ([release notes](https://sensecraft-hmi-docs.seeed.cc/en/release_note/)). **OpenDisplay** has a split-panel driver that names the E1004's T133A01 ([split_panel.h](https://github.com/OpenDisplay/Firmware/blob/main/src/split_panel.h)). But its updates come through "an update entity that flashes new firmware over Bluetooth" ([HA integration](https://github.com/OpenDisplay/Home_Assistant_Integration)), and there is no confirmed released E1004 build. No evidence was found that **Tasmota** drives this panel.

**The patched esp32-photoframe fork is the honest runner-up.** The OTA lock-in is one constant, `GITHUB_API_URL` in `main/config.h`. `ota_manager.c` requires an `https://` asset URL and flashes it with `esp_https_ota`, and there is no upload endpoint ([config.h](https://github.com/aitjcize/esp32-photoframe/blob/main/main/config.h), [ota_manager.c](https://github.com/aitjcize/esp32-photoframe/blob/main/main/ota_manager.c)). No upstream issue or PR asks for a configurable OTA source. A fork that points at its own releases is a one-line change, but it is still tied to GitHub. Adding a LAN upload endpoint, or an NVS-configurable OTA URL, is about 60 lines. Fixing the left button that blanks the screen and the "no rotation while the web UI keeps the frame awake" behaviour (the Mac Studio tab that held it awake for about 23 h) is more work on top. What you would keep is a lot: SD-card rotation with no server dependency, on-device cron, and the project's roughly monthly feature releases ([releases](https://github.com/aitjcize/esp32-photoframe/releases)). What you would pay is a rebase against upstream for every release, and a firmware that will never be one of your ESPHome fleet. Choose this path only if keeping the frame independent of the server matters more to you than fleet consistency.

## ESPHome's E1004 driver maps pure-palette PNGs exactly

The ESPHome `epaper_spi` platform got a dedicated `T133A01` driver in **PR #16706, merged 2026-07-04**. That puts it first in **2026.7.0**, which matches Seeed's statement that "E1003 and E1004 require ESPHome 2026.7.0 or later" ([PR #16706](https://github.com/esphome/esphome/pull/16706), [Seeed Display Basics](https://wiki.seeedstudio.com/reterminal_e10xx_with_esphome/)). Your fleet's 2026.10 is new enough. Four follow-up fixes have landed. The important ones are a watchdog deadlock on the last row of a transfer (#17709) and "Skip the refresh when the frame is unchanged" (#20333, merged 2026-10-23). #20333 most likely first ships in **2026.11.0**, which matters on battery because every skipped refresh saves 20–30 s of panel drive ([commit history](https://github.com/esphome/esphome/commits/dev/esphome/components/epaper_spi/epaper_spi_t133a01.cpp)). The driver uses a **960,000-byte 4bpp framebuffer in PSRAM**, and the docs give full refreshes of "around 30 seconds" ([esphome.io epaper_spi](https://esphome.io/components/display/epaper_spi/)). The model preset already handles the dual chip-select lines and the inverted busy pin.

The driver does **no dithering and no nearest-colour matching**. Near-grays (max−min < 50) become black or white by brightness. Every other colour picks an ink by which RGB channels are above 128 ([epaper_spi_t133a01.cpp](https://github.com/esphome/esphome/blob/dev/esphome/components/epaper_spi/epaper_spi_t133a01.cpp)). That would be a problem for a raw photo, but it works perfectly with esp32-photoframe's output. Its pipeline makes dithering decisions against the **measured** palette and then writes pixels in the **theoretical** colours: black, white, red, green, blue and yellow, each at pure 0/255 values ([MEASURED_PALETTE.md](https://github.com/aitjcize/esp32-photoframe/blob/main/docs/MEASURED_PALETTE.md)). Each of those lands in its own corner of the RGB cube, and stays there even after RGB565 truncation (255 becomes 248/252, which is still above 128). So the frame shows exactly what the server dithered. Two things break this. **JPEG**: in a local synthetic test, 52% of the pixels in a dithered 1200x1600 image changed ink after a q95 JPEG round trip. And any tool that writes the *measured* colours to the output file: a measured blue whose B value is ≤128 would map to black. Serve PNG. For a dithered gradient the PNG was about 0.4–0.6 MB, against 5.8 MB for a 24-bit BMP.

**The one sharp edge is the display path.** Issue #18797 found that drawing a full-resolution `online_image` directly from a display lambda hangs or crashes the E1004 in every colour mode. The maintainer explained that the decoder alone takes about 3.5 MB of PSRAM. The issue was closed with a workaround, not a fix: feed the `online_image` (RGB565, 1200x1600) into an **LVGL image widget with `update_when_display_idle: true`, `buffer_size: 25%`**, with a 60 s watchdog. The reporter has run this "reliably for several days" with rotation and deep sleep ([issue #18797](https://github.com/esphome/esphome/issues/18797)). Maintainer clydebarrow's published E1004 config uses exactly this pattern, built around a "1200x1600 PNG … dithered so it looks good on the panel's 6-color Spectra E6", together with deep sleep and button wake ([clydebarrow/sample-configs e1004](https://github.com/clydebarrow/sample-configs/tree/main/e1004)). A forum user hit a "Display already in state TRANSFER_DATA" crash when a timer-driven update collided with a manual one. That is why the display uses `update_interval: never` with one redraw per wake ([HA forum](https://community.home-assistant.io/t/reterminal-e10004/1018144)). Things ESPHome cannot do today: read images from the SD card (there is no core SD or FAT component), dither on the device (feature request [#3467](https://github.com/orgs/esphome/discussions/3467) is still open with no PR), and keep a working battery-life record. No E1004 battery measurements exist for any firmware. Seeed claims "up to 6 months" ([Seeed overview](https://wiki.seeedstudio.com/reterminal_e10xx_main_page/)).

## Reuse esp32-photoframe's processing behind a stable LAN URL

All of the image quality you like comes from `@aitjcize/epaper-image-convert`: MIT licence, version 0.1.21, updated 2026-10-04. It provides S-curve tone mapping, dynamic range compression, FS/Stucki/Burkes/Sierra dithering, and measured-palette matching in RGB or LAB ([npm](https://registry.npmjs.org/@aitjcize/epaper-image-convert), [repo](https://github.com/aitjcize/epaper-image-convert)). `process-cli --serve` already wraps it in an HTTP server. `GET /image` picks a random photo from album subfolders, applies EXIF orientation, crops, tone-maps and dithers it, and `--serve-format png` returns a PNG. `--device-config` takes the JSON you export from the current frame's web UI (Settings → Maintenance → Config Backup), which carries **your panel's calibrated palette and processing settings across to the new firmware** ([process-cli](https://github.com/aitjcize/esp32-photoframe/tree/main/process-cli)). Two things to know about running it as-is. The README pins **Node ≤20** plus the Cairo/Pango dev libraries for node-canvas, which is a Dockerfile concern. And serve mode is built for the old firmware's URL mode, so it has gaps for ESPHome: no ETag, `Math.random()` on every request (a retry or reboot shows a different picture), no folder rescan, root-level files ignored, and a default size of 800x480 unless you pass `-d 1200x1600`.

The recommended service is therefore a **thin wrapper of roughly 150–250 lines** (an estimate) around the library's `processImage`/`createPNG`, packaged as a Compose service. It pre-renders the next image on a schedule, shuffles without repeats, rescans the mounted photo folder, and exposes two endpoints: `GET /next.png` (advance, then serve) and `GET /current.png` (stable, with an ETag). The container could live in its own small repo, since this repo's CLAUDE.md asks to stay minimal. If you want the quickest possible proof first, containerise `process-cli --serve` unchanged (about 1–2 hours) and point ESPHome at `/image`. The other engines to know about are OpenDisplay's `epaper-dithering` (OKLab, optional DBS refinement) ([repo](https://github.com/OpenDisplay/epaper-dithering)) and `epdoptimize`, which ships an `aitjcizeSpectra6Palette` ([repo](https://github.com/paperlesspaper/epdoptimize)). Neither has been compared visually with your current output. Switching would throw away a look you have already tuned, so keep epaper-image-convert.

## Suggested architecture: wake, fetch one PNG, paint, sleep

**Server (Docker host):** a `photoframe-render` container (`node:20-bookworm` plus the Cairo packages) that mounts `/photos:ro` (the 12 SD images to start with, in an album subfolder; later a NAS or Immich export folder) and `/config/frame-config.json:ro` (the calibration export). Its scheduler matches today's cron (`0 6-21/2 *`): it renders the next 1200x1600 portrait PNG ahead of each slot, so a request never waits on seconds of JS dithering. It serves `/next.png` and `/current.png` on a fixed port. On pfSense, allow 192.168.20.x to reach that one port on the Docker host. Keep everything else as it is, since the frame needs no internet.

**Frame (ESPHome):** one YAML that pulls in your shared `common/` packages for Wi-Fi, encrypted `api:`, `ota: platform: esphome` and the logger. A timer wake runs this script: Wi-Fi `on_connect` → read the battery (enable GPIO21, ADC GPIO1, ×2) → `component.update` on `online_image` pointed at `/next.png` → LVGL image update → wait for `component.is_idle` on the display → compute the next even hour between 06:00 and 20:00, or 06:00 tomorrow → `deep_sleep.enter`. A button wake fetches `/current.png` or `/next.png` depending on which button was pressed. For time, use `time: platform: homeassistant` and sync the on-board PCF8563 RTC (I2C SDA 19, SCL 20, address 0x51) ([Seeed RTC/SD cookbook](https://wiki.seeedstudio.com/reterminal_e10xx_with_esphome_rtc_sd_microphone/)). That works with no internet. The skeleton below follows clydebarrow's config and the #18797 workaround. It is untested, so check the key names against the docs.

```yaml
packages: {base: !include common/base.yaml}      # wifi, api (encrypted), ota esphome, logger
esp32:
  variant: esp32s3
  flash_size: 32MB                                # confirm with: uv run esptool flash-id
  watchdog_timeout: 60s
  framework: {type: esp-idf}
psram: {mode: octal}
http_request: {timeout: 20s}
spi: {clk_pin: GPIO7, mosi_pin: GPIO9, miso_pin: GPIO8}
display:
  - {platform: epaper_spi, id: epd, model: seeed-reterminal-e1004, update_interval: never}
image:
  - platform: online_image
    id: photo
    url: http://<docker-host>:<port>/next.png
    format: PNG
    type: RGB565
    resize: 1200x1600                             # equal to native: no on-device resampling
    on_download_finished: [lvgl.image.update ..., lvgl.resume]
lvgl: {update_when_display_idle: true, paused: true, buffer_size: 25%}
deep_sleep:
  id: ds
  run_duration: {default: 120s, gpio_wakeup_reason: 10min}   # button press = OTA window
  esp32_ext1_wakeup: {pins: [GPIO3, GPIO4, GPIO5], mode: ANY_LOW}
```

**OTA without USB:** the frame ignores the network while it is asleep ([deep_sleep docs](https://esphome.io/components/deep_sleep/)), so give it two ways to open an update window. The first is an HA `input_boolean.frame_ota_mode`, mirrored as a `homeassistant` binary sensor, that triggers `deep_sleep.prevent` on the next wake. Turn it on, wait for the next wake, run `esphome run frame.yaml --device 192.168.20.x`, then turn it off. The second is a button press, whose 10-minute `gpio_wakeup_reason` run duration gives you a manual window. If you later want updates to happen on their own, ESPHome's `update: platform: http_request` can poll a manifest on the same Docker host. Its only GitHub-specific note is a warning about URL length ([update http_request](https://esphome.io/components/update/http_request/)).

## Migration runbook and the conflicts to settle on the bench

1. **Before touching flash, on the current firmware:** export the config JSON from the web UI and treat it as sensitive (it may contain credentials; never commit it). Copy the 12 photos off the SD card.
2. **Build and prove the render service:** check that a served PNG is exactly 1200x1600 and contains only the six pure colours. A quick Pillow `getcolors()` count of 6 does it. Add the pfSense rule.
3. **Write and compile the ESPHome YAML** on 2026.10.2, or on 2026.11.0 once it is out, to get #20333.
4. **The one USB session, from a Linux host if you can borrow one, since the board enumerates cleanly there as `/dev/ttyACM0`:** run `uv run esptool flash-id` (this answers the flash-size question). Then `uv run esptool read-flash 0 ALL backups/e1004-factory.bin`, and confirm the file exists and its size matches the flash size before going further. Then write ESPHome's factory image with `uv run esptool write-flash 0x0 <name>.factory.bin`. If it won't connect, hold BOOT while you plug it in. The backup contains NVS (at 0x9000) with Wi-Fi credentials, so it stays out of git.
5. **Confirm it works over the network before unplugging:** check that it appears in HA with encryption, then do one network OTA push while it is awake and USB-powered.
6. **Then test on battery:** a timer wake, each button, the OTA-mode toggle, and a 304/unchanged wake once #20333 is available. To roll back, `write-flash 0x0 backups/e1004-factory.bin` restores the device exactly.

Several facts in the notes conflict, and you should settle them on the hardware rather than trust any single source. **Button GPIO roles**: Seeed's cookbook says GPIO3 is green/"next", GPIO5 is left white/"previous" and GPIO4 is right white. clydebarrow labels GPIO5 "Refresh", GPIO4 "Left" and GPIO3 "Right". Your current firmware treats GPIO4 as the left button that clears the screen ([Seeed advanced cookbook](https://wiki.seeedstudio.com/reterminal_e10xx_with_esphome_advanced/), [picture.yaml](https://github.com/clydebarrow/sample-configs/blob/main/e1004/picture.yaml)). Flash a test config that logs each `binary_sensor` press, and assign the actions afterwards. GPIO3 is a strapping pin, so set `ignore_strapping_warning`. **Flash size**: Seeed and clydebarrow say 32 MB, while the #18797 reporter wrote "4MB flash". The 4 MB figure is probably a board default, not a measurement, and `esptool flash-id` settles it. **Minimum ESPHome version**: a forum post's "2025.11.1" is the generic `epaper_spi` minimum, while the E1004 model needs 2026.7.0. **Busy-pin polarity**: the docs' list of inverted-busy models leaves out the E1004, but the source preset sets it to inverted. Trust the preset. **The Mac driver problem**: the notes don't explain why the Mac needed a driver. A borrowed Linux box avoids the question, and it only matters for this one session.

## Conclusion

The decision turns less on firmware features than on where the "brain" of the frame lives. esp32-photoframe puts the dithering, the scheduling and the update channel on the device, so all three come from upstream's choices, including the GitHub-only OTA and the keep-alive behaviour that held your frame awake for a day. The ESPHome design moves that logic onto infrastructure you already run, and leaves the frame as a passive, sleeping display with the same image quality, because the same library and calibration produce the pixels. The trade-off is a new dependency: if the render container is down, the frame keeps its last image (e-paper holds it with no power), but it won't rotate. A health check on `/current.png` in your existing monitoring covers that risk.

The rest of the risk is in the driver's youth. It is four months old, has one closed-with-workaround display bug, and has no published battery measurements. Treat the first two weeks on battery as the real test. Record the battery percentage on every wake as an HA sensor, and compare the trend with the vendor's six-month claim before you move off the factory backup for good. Keep `backups/e1004-factory.bin` indefinitely. It is the only way back to esp32-photoframe without depending on anyone's release server.

---

## Handoff (2026-11-09)

Where the migration stands, written so another session can pick it up.

### Current state

| Piece | State |
|---|---|
| Frame | ESPHome 2026.10.1 since 2026-11-10 (was esp32-photoframe v2.20.0). Hardware: 32 MB Winbond flash, 8 MB octal PSRAM, MAC `b8:1f:3f:d7:4f:9c`, CH340 USB-serial (`/dev/ttyUSB0` on Linux, no BOOT press needed). Buttons confirmed: left GPIO4, right GPIO3, refresh GPIO5. Per wake: ~2-4 s server render, ~15 s download+decode, ~3.4 s LVGL render, ~32 s panel refresh. |
| Server | `~/docker/photoframe-server` (committed), `aitjcize/esp32-photoframe-server:v1.18.0`. UI https://photoframe-server.lan.kitcorey.com, device id 1. Frame-facing URL `http://photoframe-server.lan.kitcorey.com:9607/image` (plain HTTP on purpose: the old firmware can't validate step-ca). |
| Network | Frame 192.168.20.59 (DHCP reservation, outside pool; DNS `photoframe.lan.kitcorey.com`). pfSense NoT rule: `photoframe` -> `docker1` tcp/9607. NoT may also reach HA, pfSense DNS and NTP. No internet. |
| ESPHome | `photoframe.yml` flashed and committed. Adopted in HA by IP (mDNS doesn't cross VLANs). HA -> frame:6053 works. DNS is now the router (192.168.20.1) fleet-wide. `watchdog_timeout: 60s` added: the LVGL render blocks the loop ~3.4 s per image, close to the 5 s default. Secret `photoframe_server_auth` ("Bearer <device token>") in the gitignored root `secrets.yaml`. |
| Backups | Factory flash: `~/dev/esp32-photoframe/backups/e1004-factory.bin` on april-laptop and docker1, 33554432 bytes, sha256 `1d1954f2543919874e811e0cc3144635acb0f00b4ae2fac8d0e1c559c49224f2`. Settings: `~/dev/esp32-photoframe/backups/frame-settings-2026-11-09/` (config, processing, palette). Thumbnails of the 12 SD photos: `/mnt/tank/users/kit/frame-thumbnails-2026-11-09/`. |

### Verified facts the config relies on

- A header-less `GET /image` with only `Authorization: Bearer ...` returns `image/png`, 1200x1600 RGBA, fully opaque, exactly the six pure colors (0 off-palette pixels). The server decides PNG vs `.epdgz` from `X-Firmware-Version`; ESPHome must not send it.
- Every response carries a ~2 KB `X-Config-Payload` header (it includes the device token). `http_request.buffer_size_rx: 4096` is set because the 512 B default can't parse it. If downloads fail, check this first.
- The server applies a calibrated palette only from an `X-Color-Palette` request header. The frame's palette is the firmware default, so it isn't sent.
- ESPHome 2026.10: `online_image` is `image: - platform: online_image` (the top-level key is removed in 2027.1.0); `request_headers` values are templatable.
- `epaper_spi` E1004 driver only sends the panel a sleep command; it doesn't cut GPIO12 power. `photoframe.yml` latches GPIO12/16/21/48 low in `on_shutdown` (runs after all `on_safe_shutdown`).
- OTA with `flash_size: 32MB` requires experimental IDF features, so the config declares 16 MB.

### Decisions made

- Name `photoframe`, friendly name "Photo Frame", static IP 192.168.20.59.
- Buttons: left and right both fetch a new (random) image; refresh stays awake 10 min (for OTA/logs/HA). No clear-screen function. Power-on/reset fetches and stays awake.
- Time: `homeassistant` (fleet convention), copied to the PCF8563 RTC on sync, RTC read at boot.
- No SD card use (core ESPHome has none; the p1ngb4ck storage PR series was closed unmerged 2026-09).
- Wake via the Refresh button for updates, not an HA toggle (saves battery).

### Next steps

1. **Fleet update support for sleeping devices** (proposed, *not yet approved by the user*; don't implement without asking). Add an optional `wake: "<hint>"` field to `fleet/availability.yml` (`photoframe: wake: "Press Refresh on the frame"`). In `fleet_update.py`, a `wake:` device that doesn't answer ping is planned as "flash (needs wake)" instead of "unreachable", is flashed last (after critical devices), and the script prints the hint and waits up to ~5 min for a ping before flashing. Non-interactive: the skill asks the user to press Refresh, then runs with something like `--wait-for-wake photoframe.yml`. The existing wait-for-HA-version step works unchanged within the 10-min awake window. Touches `fleet_update.py`, the availability classifier and `SKILL.md`.
2. ~~**USB session**~~ Done 2026-11-10 from april-laptop (Linux). Original plan kept for reference: (the user's machines are Macs; the frame's USB goes through a USB-serial bridge that has needed a driver on macOS; on Linux it's `/dev/ttyACM0`). Hold BOOT while plugging in if it won't connect. On the Mac: `brew install uv`, then:
   1. `uvx esptool -p /dev/cu.<port> flash-id` and confirm 32 MB flash.
   2. `uvx esptool -p /dev/cu.<port> read-flash 0 ALL e1004-factory.bin`, check the size, copy it to `~/dev/esp32-photoframe/backups/e1004-factory.bin` on docker1. It contains NVS (Wi-Fi credentials): never commit it or put it in a synced folder.
   3. `uvx esptool -p /dev/cu.<port> write-flash 0x0 firmware.factory.bin` (from `.esphome/build/photoframe/build/`).
3. ~~**First boot checks**~~ Done 2026-11-10, all passed except the scheduled battery wake (first one due 20:00 that day). Colors may look slightly washed out compared with the old firmware; unconfirmed. Original list: a photo appears; the device joins HA as "Photo Frame" (adopt with the API key); press each button and match the logged GPIO to the physical button (sources disagree: GPIO3/4/5); run one network OTA (`uv run esphome run photoframe.yml --device 192.168.20.59`) during the awake window; confirm it sleeps and wakes on the next slot.
4. **After cutover:** regenerate the device token in the server UI (the current one was printed in a session transcript) and update `photoframe_server_auth`; watch the battery sensor for a week; optionally remove the frame's old "device config" in the server (its pushes to the frame API will fail harmlessly).

### Rollback

`write-flash 0 e1004-factory.bin` restores the stock esp32-photoframe exactly as it was (NVS included). The SD card still holds the 12 original `.epdgz` images.

### Housekeeping

- The pfSense **read-only** API key (`~/dev/pfsense/mcp/env`) was printed in a session transcript on 2026-11-09; rotate it.
