"""Buttons for pan/tilt nudges and stop."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NUDGE_SECONDS, DEFAULT_NUDGE_SECONDS, DIRECTIONS, DOMAIN

_LOGGER = logging.getLogger(__name__)

_ICONS = {
    "up": "mdi:arrow-up-bold",
    "down": "mdi:arrow-down-bold",
    "left": "mdi:arrow-left-bold",
    "right": "mdi:arrow-right-bold",
}

_NAMES = {
    "up": "Tilt up",
    "down": "Tilt down",
    "left": "Pan left",
    "right": "Pan right",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    serial = data["info"].get("serial") or entry.entry_id
    options = {**entry.data, **entry.options}
    seconds = float(options.get(CONF_NUDGE_SECONDS, DEFAULT_NUDGE_SECONDS))
    client = data["client"]

    entities: list[ButtonEntity] = [
        HikBridgeStopButton(client, serial), *[
            HikBridgeMoveButton(client, serial, direction, seconds)
            for direction in DIRECTIONS
        ]
    ]
    async_add_entities(entities)


def _device(serial: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name="Hikvision PTZ",
        manufacturer="Hikvision / EZVIZ OEM",
        model="PTZ camera (HCNetSDK)",
        serial_number=serial,
    )


class HikBridgeMoveButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, client, serial: str, direction: str, seconds: float) -> None:
        self._client = client
        self._direction = direction
        self._seconds = seconds
        self._attr_unique_id = f"{serial}_move_{direction}"
        self._attr_name = _NAMES[direction]
        self._attr_icon = _ICONS[direction]
        self._attr_device_info = _device(serial)

    async def async_press(self) -> None:
        await self._client.ptz(self._direction, self._seconds)


class HikBridgeStopButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Stop"
    _attr_icon = "mdi:stop"

    def __init__(self, client, serial: str) -> None:
        self._client = client
        self._attr_unique_id = f"{serial}_stop"
        self._attr_device_info = _device(serial)

    async def async_press(self) -> None:
        await self._client.stop()
