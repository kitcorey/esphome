# ESPHome Device Configurations

ESPHome YAML configs for devices integrated into Home Assistant via native API.

## Conventions

- **Packages**: Configs use `packages:` to include shared settings from `common/` (e.g., `common/wifi.yml`, `common/sensor_wifi.yml`)
- **Secrets**: All secrets in `common/secrets.yaml` — referenced via `!secret`
- **Substitutions**: Each config defines `name`, `friendly_name`, `static_ip`, `reboot_timeout` in a `substitutions:` block
- **API encryption**: All devices use `api: encryption: key: !secret api_encryption_key`
- **File naming**: Kebab-case YAML files (e.g., `bedroom-headboard.yml`, `mitsubishi-huzzah32.yaml`)

## Deploying

No ESPHome dashboard or container — devices are managed individually via CLI:

```sh
esphome run <config>.yaml     # Compile + flash (USB first time, OTA after)
esphome logs <config>.yaml    # Stream logs over WiFi
esphome compile <config>.yaml # Compile only (validate config)
```

## Network

- IoT devices live on the `192.168.20.x` subnet
- WiFi is hidden SSID, WPA2 minimum
- HA connects to ESPHome devices via native API on port 6053
