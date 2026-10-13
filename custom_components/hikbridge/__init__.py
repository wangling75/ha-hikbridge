"""Hikvision PTZ Bridge integration.

Talks to the `hikbridge` add-on, which owns the vendor HCNetSDK session. One
config entry drives one camera on the bridge; add the integration again (same
host/port, different camera) to expose a second camera.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_extract_config_entry_ids
from homeassistant.helpers.typing import ConfigType

from .api import HikBridgeAuthError, HikBridgeClient, HikBridgeError
from .const import (
    CONF_CAMERA_ID,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    CONF_TTS_ENTITY,
    DEFAULT_NUDGE_SECONDS,
    DEFAULT_PORT,
    DIRECTIONS,
    DOMAIN,
    SERVICE_MOVE,
    SERVICE_SPEAK,
    SERVICE_STOP,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CAMERA, Platform.BUTTON, Platform.MEDIA_PLAYER]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

MOVE_SCHEMA = vol.Schema(
    {
        vol.Required("direction"): vol.In(DIRECTIONS),
        vol.Optional("duration", default=DEFAULT_NUDGE_SECONDS): vol.All(
            vol.Coerce(float), vol.Range(min=0.05, max=10)
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

SPEAK_SCHEMA = vol.Schema(
    {
        vol.Optional("text"): cv.string,
        vol.Optional("media_url"): cv.string,
        vol.Optional("tts_entity"): cv.entity_id,
        vol.Optional("language"): cv.string,
        vol.Optional("cache", default=False): cv.boolean,
    },
    extra=vol.ALLOW_EXTRA,
)


def _entry_data(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    return {**entry.data, **entry.options}


def build_client(hass: HomeAssistant, entry: ConfigEntry) -> HikBridgeClient:
    data = _entry_data(hass, entry)
    return HikBridgeClient(
        async_get_clientsession(hass),
        data[CONF_HOST],
        data.get(CONF_PORT, DEFAULT_PORT),
        data.get(CONF_TOKEN),
        data.get(CONF_CAMERA_ID),
    )


def select_camera(info: dict, camera_id: str | None) -> dict:
    """Pick the camera described by this config entry.

    Older add-on versions do not return a camera list at all, in which case
    everything falls back to the top level serial/device of /health.
    """
    cameras = info.get("cameras") or []
    if camera_id:
        for camera in cameras:
            if str(camera.get("id")) == str(camera_id):
                return dict(camera)
    if cameras:
        return dict(cameras[0])
    return {
        "id": camera_id,
        "name": None,
        "serial": info.get("serial"),
        "device": info.get("device") or {},
    }


def entry_camera(hass: HomeAssistant, entry_id: str) -> dict:
    return hass.data.get(DOMAIN, {}).get(entry_id, {}).get("camera") or {}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = _entry_data(hass, entry)
    client = build_client(hass, entry)
    try:
        info = await client.health()
    except HikBridgeAuthError as exc:
        raise HomeAssistantError(f"hikbridge token rejected: {exc}") from exc
    except HikBridgeError as exc:
        raise HomeAssistantError(
            f"cannot reach the hikbridge add-on: {exc}"
        ) from exc

    camera = select_camera(info, data.get(CONF_CAMERA_ID))
    _LOGGER.info(
        "hikbridge connected: camera=%s serial=%s version=%s",
        camera.get("id") or "(legacy)", camera.get("serial"), info.get("version"),
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "info": info,
        "camera": camera,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_MOVE):
        _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            for service in (SERVICE_MOVE, SERVICE_STOP, SERVICE_SPEAK):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------
async def _clients_for_call(hass: HomeAssistant, call: ServiceCall) -> list[HikBridgeClient]:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("no hikbridge camera is configured")
    try:
        target_ids = await async_extract_config_entry_ids(hass, call)
    except Exception:  # noqa: BLE001 - targeting is optional
        target_ids = set()
    if target_ids:
        selected = [entries[eid] for eid in target_ids if eid in entries]
    else:
        selected = list(entries.values())
    if not selected:
        raise HomeAssistantError("no matching hikbridge camera")
    return [item["client"] for item in selected]


def _first_entry_id(hass: HomeAssistant) -> str | None:
    entries = hass.data.get(DOMAIN, {})
    return next(iter(entries), None)


async def _tts_to_wav(hass: HomeAssistant, text: str, entity_id: str,
                      language: str | None) -> bytes:
    from homeassistant.components import tts

    options = {
        tts.ATTR_PREFERRED_FORMAT: "wav",
        tts.ATTR_PREFERRED_SAMPLE_RATE: 16000,
        tts.ATTR_PREFERRED_SAMPLE_CHANNELS: 1,
        tts.ATTR_PREFERRED_SAMPLE_BYTES: 2,
    }
    stream = tts.async_create_stream(hass, entity_id, language, options)
    stream.async_set_message(text)
    data = b"".join([chunk async for chunk in stream.async_stream_result()])
    extension = stream.extension
    if not data:
        raise HomeAssistantError(f"{entity_id} returned no audio")
    if extension == "wav":
        return data
    return await _convert_audio(hass, data, extension or "mp3")


async def _convert_audio(hass: HomeAssistant, data: bytes, extension: str) -> bytes:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise HomeAssistantError(
            f"TTS returned .{extension} audio and ffmpeg is not installed; use a "
            "WAV-capable TTS engine (for example Piper) or pass media_url with a WAV file"
        )
    proc = await asyncio.create_subprocess_exec(
        binary, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-f", "wav", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(data)
    if proc.returncode != 0 or not out:
        raise HomeAssistantError(
            f"ffmpeg could not convert .{extension} audio: {err.decode(errors='replace')[:200]}"
        )
    return out


def _async_register_services(hass: HomeAssistant) -> None:
    async def handle_move(call: ServiceCall) -> None:
        for client in await _clients_for_call(hass, call):
            await client.ptz(call.data["direction"], call.data["duration"])

    async def handle_stop(call: ServiceCall) -> None:
        for client in await _clients_for_call(hass, call):
            await client.stop()

    async def handle_speak(call: ServiceCall) -> None:
        text = call.data.get("text")
        media_url = call.data.get("media_url")
        if not text and not media_url:
            raise HomeAssistantError("speak needs either text or media_url")

        if media_url:
            wav = await _download(hass, media_url)
        else:
            entity_id = call.data.get("tts_entity")
            if not entity_id:
                entry_id = _first_entry_id(hass)
                entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
                entity_id = (
                    _entry_data(hass, entry).get(CONF_TTS_ENTITY) if entry else None
                )
            if not entity_id:
                raise HomeAssistantError(
                    "no tts_entity given and none configured on the integration"
                )
            wav = await _tts_to_wav(
                hass, text, entity_id, call.data.get("language")
            )

        for client in await _clients_for_call(hass, call):
            await client.speak_wav(wav)

    hass.services.async_register(DOMAIN, SERVICE_MOVE, handle_move, schema=MOVE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP, handle_stop)
    hass.services.async_register(DOMAIN, SERVICE_SPEAK, handle_speak, schema=SPEAK_SCHEMA)


async def _download(hass: HomeAssistant, url: str) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise HomeAssistantError("media_url must be an http(s) URL")
    session = async_get_clientsession(hass)
    async with session.get(url, timeout=30) as resp:
        if resp.status >= 400:
            raise HomeAssistantError(f"cannot fetch {url}: HTTP {resp.status}")
        return await resp.read()
