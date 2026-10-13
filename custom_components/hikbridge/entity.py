"""Shared entity helpers for the hikbridge platforms."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def camera_serial(camera: dict, fallback: str) -> str:
    """Stable identifier for one camera on the bridge."""
    return str(camera.get("serial") or camera.get("id") or fallback)


def device_info(camera: dict, fallback: str) -> DeviceInfo:
    info: DeviceInfo = {
        "identifiers": {(DOMAIN, camera_serial(camera, fallback))},
        "name": camera.get("name") or "Hikvision PTZ",
        "manufacturer": "Hikvision / EZVIZ OEM",
        "model": "PTZ camera (HCNetSDK)",
    }
    serial = camera.get("serial")
    if serial:
        info["serial_number"] = str(serial)
    return info
