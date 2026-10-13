"""Config and options flow for the Hikvision PTZ Bridge."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HikBridgeAuthError, HikBridgeClient, HikBridgeError
from .const import (
    CONF_CAMERA_ID,
    CONF_HOST,
    CONF_NUDGE_SECONDS,
    CONF_PORT,
    CONF_RTSP_URL,
    CONF_TOKEN,
    CONF_TTS_ENTITY,
    DEFAULT_NUDGE_SECONDS,
    DEFAULT_PORT,
    DOMAIN,
)


def _schema(defaults: dict | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "127.0.0.1")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_TOKEN, default=defaults.get(CONF_TOKEN, "")): str,
            vol.Optional(CONF_RTSP_URL, default=defaults.get(CONF_RTSP_URL, "")): str,
            vol.Optional(CONF_TTS_ENTITY, default=defaults.get(CONF_TTS_ENTITY, "")): str,
            vol.Optional(
                CONF_NUDGE_SECONDS,
                default=defaults.get(CONF_NUDGE_SECONDS, DEFAULT_NUDGE_SECONDS),
            ): vol.Coerce(float),
        }
    )


async def _validate(hass: HomeAssistant, data: dict) -> tuple[dict, list[dict]]:
    """Return (health, cameras) for the bridge at this address."""
    client = HikBridgeClient(
        async_get_clientsession(hass),
        data[CONF_HOST],
        data[CONF_PORT],
        data.get(CONF_TOKEN),
    )
    info = await client.health()
    cameras = [c for c in (info.get("cameras") or []) if isinstance(c, dict)]
    if not cameras:
        # Older add-on without camera ids: behave like a single camera.
        cameras = [{"id": "", "name": None, "serial": info.get("serial")}]
    return info, cameras


def _label(camera: dict) -> str:
    name = camera.get("name") or camera.get("id") or "camera"
    serial = camera.get("serial")
    return f"{name} ({serial})" if serial else str(name)


def _camera_schema(cameras: list[dict], default: str | None = None) -> vol.Schema:
    choices = {c.get("id") or "": _label(c) for c in cameras}
    return vol.Schema(
        {vol.Required(CONF_CAMERA_ID, default=default or next(iter(choices))): vol.In(choices)}
    )


class HikBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}
        self._info: dict = {}
        self._cameras: list[dict] = []

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {k: v for k, v in user_input.items() if v not in ("", None)}
            try:
                info, cameras = await _validate(self.hass, data)
            except HikBridgeAuthError:
                errors["base"] = "invalid_auth"
            except HikBridgeError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                self._data, self._info, self._cameras = data, info, cameras
                if len(self._cameras) > 1:
                    return await self.async_step_camera()
                return await self._create(data, self._cameras[0])

        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input), errors=errors
        )

    async def async_step_camera(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            wanted = user_input.get(CONF_CAMERA_ID)
            camera = next(
                (c for c in self._cameras if (c.get("id") or "") == wanted),
                self._cameras[0],
            )
            data = dict(self._data)
            if camera.get("id"):
                data[CONF_CAMERA_ID] = camera["id"]
            return await self._create(data, camera)

        return self.async_show_form(
            step_id="camera", data_schema=_camera_schema(self._cameras)
        )

    async def _create(self, data: dict, camera: dict) -> FlowResult:
        serial = (
            camera.get("serial") or camera.get("id")
            or f"{data[CONF_HOST]}:{data[CONF_PORT]}"
        )
        await self.async_set_unique_id(str(serial))
        self._abort_if_unique_id_configured()
        title = camera.get("name") or camera.get("id") or serial
        return self.async_create_entry(title=f"Hikvision PTZ ({title})", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        return HikBridgeOptionsFlow(entry)


class HikBridgeOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        self._data: dict = {}
        self._cameras: list[dict] = []

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        current = {**self.entry.data, **self.entry.options}
        if user_input is not None:
            data = {k: v for k, v in user_input.items() if v not in ("", None)}
            try:
                _info, cameras = await _validate(self.hass, data)
            except HikBridgeAuthError:
                errors["base"] = "invalid_auth"
            except HikBridgeError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                self._data, self._cameras = data, cameras
                if len(cameras) > 1:
                    return await self.async_step_camera()
                if cameras and cameras[0].get("id"):
                    data[CONF_CAMERA_ID] = cameras[0]["id"]
                return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="init", data_schema=_schema(current), errors=errors
        )

    async def async_step_camera(self, user_input: dict | None = None) -> FlowResult:
        current = {**self.entry.data, **self.entry.options}
        if user_input is not None:
            data = dict(self._data)
            data[CONF_CAMERA_ID] = user_input.get(CONF_CAMERA_ID)
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="camera",
            data_schema=_camera_schema(self._cameras, current.get(CONF_CAMERA_ID)),
        )
