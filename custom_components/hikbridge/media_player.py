"""Media player entity for the camera speaker."""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from homeassistant.components import media_source, tts
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from . import _convert_audio, _download
from .const import DOMAIN
from .entity import device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    camera = data.get("camera") or {}
    async_add_entities(
        [HikBridgeSpeaker(data["client"], camera, entry.entry_id)]
    )


class HikBridgeSpeaker(MediaPlayerEntity):
    """A speaker endpoint backed by the camera's two-way audio channel."""

    _attr_has_entity_name = True
    _attr_name = "Speaker"
    _attr_icon = "mdi:speaker"
    _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA
    _attr_state = MediaPlayerState.IDLE

    def __init__(self, client, camera: dict, entry_id: str) -> None:
        self._client = client
        serial = camera.get("serial") or camera.get("id") or entry_id
        self._attr_unique_id = f"{serial}_speaker"
        self._attr_device_info = device_info(camera, entry_id)

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs
    ) -> None:
        """Resolve, convert and play media through the camera speaker."""
        mime_type = media_type or ""
        url = media_id

        _LOGGER.debug(
            "play_media called: media_type=%s media_id=%s", media_type, media_id
        )

        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()
        try:
            if media_id.startswith("media-source://tts/"):
                extension, data = await tts.async_get_media_source_audio(
                    self.hass, media_id
                )
                _LOGGER.debug(
                    "TTS generated %d bytes as .%s", len(data), extension
                )
                if extension != "wav":
                    data = await _convert_audio(
                        self.hass, data, extension or "mp3"
                    )
            else:
                if media_id.startswith("media-source://"):
                    resolved = await media_source.async_resolve_media(
                        self.hass, media_id, self.entity_id
                    )
                    url = resolved.url
                    mime_type = resolved.mime_type or mime_type

                if url.startswith("/"):
                    base = get_url(
                        self.hass, prefer_external=False, allow_internal=True
                    )
                    url = f"{base.rstrip('/')}{url}"

                if not url.startswith(("http://", "https://")):
                    raise HomeAssistantError(f"unsupported media URL: {url}")

                _LOGGER.debug("Downloading media from %s", url)
                data = await _download(self.hass, url)
                if not _is_wav(mime_type, url):
                    extension = _audio_extension(mime_type, url)
                    data = await _convert_audio(self.hass, data, extension)

            await self._client.speak_wav(data)
        except Exception:
            _LOGGER.exception("Could not play media on the camera speaker")
            raise
        finally:
            self._attr_state = MediaPlayerState.IDLE
            self.async_write_ha_state()


def _is_wav(mime_type: str, url: str) -> bool:
    if mime_type.lower() in ("audio/wav", "audio/x-wav", "audio/wave"):
        return True
    return urlparse(url).path.lower().endswith(".wav")


def _audio_extension(mime_type: str, url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".wav"):
        return "wav"
    if path.endswith(".ogg"):
        return "ogg"
    if path.endswith(".mp3"):
        return "mp3"
    if "mpeg" in mime_type.lower():
        return "mp3"
    return (mime_type.split("/", 1)[-1] or "mp3").lower()
