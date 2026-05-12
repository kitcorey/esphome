# ESPHome Configurations

ESPHome YAML configs for the devices on my home network, integrated into Home Assistant via the native API. Each device gets one YAML at the repo root; shared bits (Wi-Fi, sensor base, sunrise effect, S31 power, etc.) live in `common/`.

## Layout

```
.
├── *.yml / *.yaml      # one file per device (kebab-case)
├── common/             # reusable packages (wifi.yml, sensor_wifi.yml, sunrise.yml, …)
├── secrets.yaml        # gitignored — Wi-Fi creds, API keys, OTA passwords
├── scripts/            # helper scripts (BLE list, MAC lookup, sunrise validator)
│   └── hooks/          # versioned git hooks (delegate to hk)
├── hk.pkl              # hk hook config (pre-commit, pre-push)
├── mise.toml           # tool versions + tasks
└── pyproject.toml      # uv-managed Python deps (esphome, aioesphomeapi, …)
```

## First-time setup

1. `mise install` — installs Python 3.13, uv, gitleaks, hk, pkl.
2. `mise run install-hooks` — points `core.hooksPath` at `scripts/hooks` so hk runs on commit and push.
3. `uv sync` — installs `esphome` and the rest of the Python deps.
4. Drop your real `secrets.yaml` in the repo root (it's gitignored). See `common/wifi.yml` for the keys it expects.

## Working with a device

All `esphome` invocations go through `uv run` so they pick up the pinned version from `pyproject.toml`:

```sh
uv run esphome compile <config>.yml   # validate + build
uv run esphome run     <config>.yml   # build + upload (USB first time, OTA after)
uv run esphome logs    <config>.yml   # stream logs over the network
uv run esphome clean   <config>.yml   # nuke build artifacts for one config
```

`clean.sh` and `update.sh` are convenience wrappers for batch operations.

## Conventions

- **Packages over copy-paste.** New configs should `packages:` in the relevant `common/*.yml` rather than re-declaring Wi-Fi, API, OTA, etc.
- **Secrets via `!secret`.** Never hard-code a credential — gitleaks runs pre-commit and will block the push.
- **Substitutions block.** Each device sets `name`, `friendly_name`, `static_ip`, and `reboot_timeout` so the shared packages can reference them.
- **API encryption everywhere.** `api.encryption.key: !secret api_encryption_key`.
- **Kebab-case filenames.** `bedroom-headboard.yml`, not `BedroomHeadboard.yaml`.

## Network

- IoT devices live on `192.168.20.x` (separate VLAN from the main LAN).
- Wi-Fi SSID is hidden, WPA2 minimum — `common/wifi.yml` sets `fast_connect: true` accordingly.
- Home Assistant talks to each device on TCP 6053 (native API).

## Secret scanning

Hooks are orchestrated by [hk](https://hk.jdx.dev), configured in `hk.pkl`. The `pre-commit` hook runs `gitleaks` on staged changes; the `pre-push` hook runs it across full history.

```sh
mise exec -- hk run check        # manual staged scan
mise exec -- hk run pre-push     # manual full-history scan
```

The hk version in `mise.toml` is pinned to match the `hk@x.y.z` URLs in `hk.pkl` — bump both together.
