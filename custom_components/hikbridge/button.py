"""Buttons for pan/tilt nudges and stop."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NUDGE_SECONDS, DEFAULT_NUDGE_SECONDS, DIRECTIONS, DOMAIN
from .entity import camera_serial, device_info

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
    camera = data.get("camera") or {}
    serial = camera_serial(camera, entry.entry_id)
    options = {**entry.data, **entry.options}
    seconds = float(options.get(CONF_NUDGE_SECONDS, DEFAULT_NUDGE_SECONDS))
    client = data["client"]

    entities: list[ButtonEntity] = [
        HikBridgeStopButton(client, camera, entry.entry_id), *[
            HikBridgeMoveButton(client, camera, entry.entry_id, direction, seconds)
            for direction in DIRECTIONS
        ]
    ]
    async_add_entities(entities)
    _LOGGER.debug("added PTZ buttons for %s", serial)


class HikBridgeMoveButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, client, camera: dict, entry_id: str, direction: str,
                 seconds: float) -> None:
        self._client = client
        self._direction = direction
        self._seconds = seconds
        self._attr_unique_id = f"{camera_serial(camera, entry_id)}_move_{direction}"
        self._attr_name = _NAMES[direction]
        self._attr_icon = _ICONS[direction]
        self._attr_device_info = device_info(camera, entry_id)

    async def async_press(self) -> None:
        await self._client.ptz(self._direction, self._seconds)


class HikBridgeStopButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Stop"
    _attr_icon = "mdi:stop"

    def __init__(self, client, camera: dict, entry_id: str) -> None:
        self._client = client
        self._attr_unique_id = f"{camera_serial(camera, entry_id)}_stop"
        self._attr_device_info = device_info(camera, entry_id)

    async def async_press(self) -> None:
        await self._client.stop()
