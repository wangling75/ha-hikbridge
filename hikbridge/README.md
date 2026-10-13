# Hikvision PTZ Bridge (Home Assistant add-on)

Wraps the official Hikvision **HCNetSDK** (linux/amd64) so a Home Assistant
installation can drive cameras that only speak the Hikvision private protocol
on TCP 8000 - no HTTP, ONVIF or ISAPI service is required.

The add-on exists because Home Assistant OS runs its Core container on Alpine
(musl) and the vendor SDK is linked against glibc. The add-on is built from
Debian, so the SDK loads there, and it is reachable from Home Assistant on
`http://127.0.0.1:8654` because both use host networking.

## What it does

* PTZ nudge/stop for every configured camera
* JPEG snapshots straight from the SDK (also used by the HA camera entity)
* Speaker playback: WAV/PCM pushed to the camera speaker (`/speak`)
* Live two-way talk over a WebSocket (`/talk`) - the built-in web UI streams
  the browser microphone, or an audio file, straight to the camera speaker
* Built-in web UI at `http://<ha-host>:8654/` with camera tabs, snapshot
  preview/download, PTZ pad and push-to-talk

## Install

1. Add this repository to the add-on store and install **Hikvision PTZ
   Bridge**.
2. Open the add-on configuration and fill in the `cameras` list.
3. Start the add-on, then open `http://<ha-host>:8654/` for the web UI.
4. Install the matching custom integration (`custom_components/hikbridge`) and
   point it at `127.0.0.1:8654`.

The add-on also registers a sidebar panel through Home Assistant **ingress**,
so the same UI can be opened from the HA sidebar. The direct address
`http://<ha-host>:8654/` keeps working because host networking is enabled.

### Password

Set `auth_token` to any password. The web UI then shows a login screen
before it renders anything, and every API call needs the same token. The
Home Assistant integration has to be given the same value (it is part of the
integration options), otherwise the entities stop working.

## Options

```yaml
cameras:
  - id: cam207
    name: "Camera 207"
    camera_ip: 192.168.2.207
    camera_port: 8000
    username: admin
    password: "..."
    channel: 1
    rtsp_url: "rtsp://admin:...@192.168.2.207:554/h264/ch1/main/av_stream"
  - id: cam225
    name: "Camera 225"
    camera_ip: 192.168.2.225
    camera_port: 8000
    username: admin
    password: "..."
    channel: 1
speak_sample_rate: 16000
auth_token: ""
```

| Option | Meaning |
| --- | --- |
| `cameras` | List of cameras, each with the fields above |
| `speak_sample_rate` | PCM rate pushed to the speaker, `8000` or `16000` |
| `auth_token` | Optional shared secret for the HTTP API and web UI |

The legacy single-camera keys (`camera_ip`, `camera_port`, `username`,
`password`, `channel`) still work and are used when `cameras` is empty.

## API

```
GET  /                                    built-in web UI
GET  /health                              {"ok": true, "cameras": [...]}
GET  /api/cameras                         camera list
GET  /api/cameras/<id>/snapshot.jpg       image/jpeg (?download=1 saves it)
POST /api/cameras/<id>/ptz                {"direction": "right", "duration": 0.6}
POST /api/cameras/<id>/ptz/start          {"direction": "right"}
POST /api/cameras/<id>/ptz/stop
POST /api/cameras/<id>/speak              body = WAV file
POST /api/cameras/<id>/speak_raw?rate=8000  body = raw PCM
GET  /api/cameras/<id>/talk               WebSocket: PCM16 mono in, live talk
```

The original single-camera paths (`/ptz`, `/ptz/stop`, `/snapshot.jpg`,
`/speak`, `/speak_raw`) still exist and target the first camera.

Directions: `up`, `down`, `left`, `right`.

When `auth_token` is set, requests must carry either an `X-Auth-Token` header
or a `?token=` query parameter (the query parameter is what the web UI and
browser WebSockets use).

This camera family rejects diagonals, presets and zoom/focus with err 11, so
only the four cardinal moves are exposed.
