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
  - the add-on reads the camera's intercom codec after login and encodes
    AAC/G.711 automatically (the tested 207 camera uses AAC/16 kHz)
* Live two-way talk over a WebSocket (`/talk`) - the built-in web UI streams
  the browser microphone, or an audio file, straight to the camera speaker
* Built-in web UI at `http://<ha-host>:8654/` with camera tabs,
  snapshot fallback/download, PTZ pad and click-to-toggle talk
* Standalone WebRTC playback through the bundled go2rtc gateway, with an
  automatic fragmented-MP4 fallback (`/live.mp4`) when WebRTC is unavailable

WebRTC does not require Scrypted. The add-on starts go2rtc on media port
`8555/tcp+udp`, keeps the go2rtc API on `127.0.0.1:1984`, and proxies only
the authenticated SDP exchange through Hikbridge. Browsers cannot play an
`rtsp://` URL directly; Home Assistant's RTSP cards also relay the stream
server-side. Cameras without an audio track (for example the 225) show
"no audio track" and do not enable talk.

The bundled `go2rtc` binary is upstream `v1.9.14` from
[AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc) (MIT license).

The browser live stream is fragmented every 200 ms instead of once per
camera keyframe. The UI keeps only a few seconds of MSE buffer and
reconnects if the stream falls too far behind, which keeps video and
camera audio near the live edge without polling snapshots at the same
time.

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

## Standalone Docker

The same image can run outside Home Assistant OS without Supervisor. Use
`docker run` or `docker compose`, mount `/data/options.json`, expose port
`8654` plus `8555/tcp` and `8555/udp`, and install the custom integration separately. See the repository
root `README.md` section `纯 Docker 部署` for the complete Chinese guide,
`options.example.json`, and `docker-compose.example.yml`.

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
ssl: false
ssl_port: 8655
certfile: ""
keyfile: ""
webrtc: true
webrtc_port: 8555
webrtc_api_port: 1984
webrtc_candidates: []
```

| Option | Meaning |
| --- | --- |
| `cameras` | List of cameras, each with the fields above |
| `speak_sample_rate` | Fallback input PCM rate; the camera-reported codec/rate takes precedence |
| `auth_token` | Optional shared secret for the HTTP API and web UI |
| `ssl` | Serve the UI over HTTPS with a self-signed certificate so the browser microphone works |
| `ssl_port` | Port for the HTTPS listener, default `8655`; plain HTTP stays on `8654` |
| `certfile` / `keyfile` | Optional certificate pair; generated under `/data/ssl` when empty |
| `webrtc` | Enable the bundled WebRTC playback path, default `true` |
| `webrtc_port` | go2rtc media port for TCP and UDP, default `8555` |
| `webrtc_api_port` | Loopback-only go2rtc API port, default `1984` |
| `webrtc_candidates` | Extra advertised WebRTC addresses; required as `host-ip:8555` for bridged Docker port mapping |

Browsers only expose `navigator.mediaDevices` on secure origins, so the web
UI press-to-talk button cannot record over plain HTTP. Turn `ssl` on and open
`https://<host>:8655/`, accept the self-signed certificate warning once, and
the button starts working. The plain HTTP listener stays on `8654`, so the
Home Assistant ingress panel plus the `speak` and `/talk` endpoints used by
Scrypted keep working unchanged.

`cameras` is required and must contain at least one entry. The top-level
single-camera keys from older releases are no longer accepted.

## API

```
GET  /                                    built-in web UI
GET  /health                              {"ok": true, "cameras": [...]}
GET  /api/cameras                         camera list
GET  /api/cameras/<id>/snapshot.jpg       image/jpeg (?download=1 saves it)
GET  /api/cameras/<id>/live.mp4           fragmented MP4: RTSP video + AAC audio
POST /api/cameras/<id>/webrtc             {"sdp": "..."} -> WebRTC SDP answer
POST /api/cameras/<id>/ptz                {"direction": "right", "duration": 0.6}
POST /api/cameras/<id>/ptz/start          {"direction": "right"}
POST /api/cameras/<id>/ptz/stop
POST /api/cameras/<id>/speak              body = WAV file
POST /api/cameras/<id>/speak_raw?rate=8000  body = raw PCM16, encoded for the camera
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
