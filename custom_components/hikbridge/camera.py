"""Camera entity: SDK snapshot, RTSP stream for live view."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_RTSP_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    info = data["info"]
    options = {**entry.data, **entry.options}
    async_add_entities(
        [HikBridgeCamera(data["client"], info.get("serial") or entry.entry_id,
                         options.get(CONF_RTSP_URL))]
    )


class HikBridgeCamera(Camera):
    """Snapshot comes from the vendor SDK, live view from RTSP."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, client, serial: str, rtsp_url: str | None) -> None:
        self._client = client
        self._rtsp_url = rtsp_url
        self._attr_unique_id = f"{serial}_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name="Hikvision PTZ",
            manufacturer="Hikvision / EZVIZ OEM",
            model="PTZ camera (HCNetSDK)",
            serial_number=serial,
        )
        if rtsp_url:
            self._attr_supported_features = CameraEntityFeature.STREAM

    @property
    def stream_source(self) -> str | None:
        return self._rtsp_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        try:
            return await self._client.snapshot()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("snapshot failed: %s", exc)
            return None
