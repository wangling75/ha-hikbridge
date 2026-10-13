"""Camera entity: SDK snapshot, RTSP stream for live view."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_RTSP_URL, DOMAIN
from .entity import camera_serial, device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    camera = data.get("camera") or {}
    options = {**entry.data, **entry.options}
    async_add_entities(
        [HikBridgeCamera(data["client"], camera, entry.entry_id,
                         options.get(CONF_RTSP_URL))]
    )


class HikBridgeCamera(Camera):
    """Snapshot comes from the vendor SDK, live view from RTSP."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, client, camera: dict, entry_id: str,
                 rtsp_url: str | None) -> None:
        super().__init__()
        self._client = client
        self._rtsp_url = rtsp_url
        self._attr_unique_id = f"{camera_serial(camera, entry_id)}_camera"
        self._attr_device_info = device_info(camera, entry_id)
        if rtsp_url:
            self._attr_supported_features = CameraEntityFeature.STREAM

    async def stream_source(self) -> str | None:
        return self._rtsp_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        try:
            return await self._client.snapshot()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("snapshot failed: %s", exc)
            return None
