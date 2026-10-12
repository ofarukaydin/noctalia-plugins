# Noctalia plugins

Plugins for [Noctalia 5](https://github.com/noctalia-dev).

| Plugin | Description |
|---|---|
| [`aidot-lights`](aidot-lights) | Control AiDot / Linkind / OREiN Wi-Fi bulbs from the bar: on/off, brightness, white/colour, music sync (Beat, Smooth, Melody, Auto). Talks to bulbs over the LAN, or through a local python-matter-server when the bulbs are Matter-paired. |
| [`cpa-quota`](cpa-quota) | Codex and Claude account quotas from a CLIProxyAPI proxy with the CPA Quota API Extension, in a bar widget and popup. |

## Install

Add this repository as a plugin source in `~/.local/state/noctalia/settings.toml`:

```toml
[[plugins.source]]
kind = "git"
location = "https://github.com/ofarukaydin/noctalia-plugins"
name = "buildpulsar"
```

Then enable `buildpulsar/aidot-lights` and/or `buildpulsar/cpa-quota` in Noctalia Settings → Plugins. For local development, use `kind = "path"` pointing at a checkout instead.

## aidot-lights setup

Needs `python3` with `cryptography`. Music sync also needs `numpy` and `parec` (PulseAudio/PipeWire); `aubio` is optional and improves tempo tracking.

Log in once so the plugin can fetch each bulb's local key (the AiDot cloud is only used for this):

```sh
python3 -I aidot-lights/aidot.py login
```

Credentials are written to `~/.config/noctalia/secrets/aidot.json` (mode 0600).

## License

MIT
