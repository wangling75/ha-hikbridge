# Hikvision PTZ Bridge (Home Assistant add-on)

Wraps the official Hikvision **HCNetSDK** (linux/amd64) so a Home Assistant
installation can drive a camera that only speaks the Hikvision private
protocol on TCP 8000 - no HTTP, ONVIF or ISAPI service is required.

The add-on exists because Home Assistant OS runs its Core container on Alpine
(musl) and the vendor SDK is linked against glibc. The add-on is built from
Debian, so the SDK loads there, and it is reachable from Home Assistant on
`http://127.0.0.1:8654` because both use host networking.

## Install

1. Copy this folder into `/addons/hikbridge` on the Home Assistant machine
   (use the Samba or SSH add-on).
2. Settings -> Add-ons -> Add-on Store -> three-dot menu -> **Check for
   updates**. "Hikvision PTZ Bridge" appears under *Local add-ons*.
3. Open it, fill in the camera IP, username and password, then **Start**.
4. Install the matching custom integration (`custom_components/hikbridge`) and
   point it at `127.0.0.1:8654`.

## Options

| Option | Meaning |
| --- | --- |
| `camera_ip` | Camera address, e.g. `192.168.2.207` |
| `camera_port` | Private protocol port, default `8000` |
| `username` / `password` | Device credentials |
| `channel` | Control channel, `1` for this camera family |
| `speak_sample_rate` | PCM rate pushed to the speaker, `8000` or `16000` |
| `auth_token` | Optional shared secret for the local HTTP API |

## API

```
GET  /health
GET  /snapshot.jpg
POST /ptz          {"direction": "right", "duration": 0.6}
POST /ptz/stop
POST /speak        body = WAV file
POST /speak_raw?rate=8000   body = raw PCM, forwarded verbatim
```

Directions: `up`, `down`, `left`, `right`.

This camera family rejects diagonals, presets and zoom/focus with err 11, so
only the four cardinal moves are exposed.
