"""Config and options flow for the Hikvision PTZ Bridge."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HikBridgeAuthError, HikBridgeClient, HikBridgeError
from .const import (
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


async def _validate(hass: HomeAssistant, data: dict) -> dict:
    client = HikBridgeClient(
        async_get_clientsession(hass),
        data[CONF_HOST],
        data[CONF_PORT],
        data.get(CONF_TOKEN),
    )
    info = await client.health()
    return info


class HikBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {k: v for k, v in user_input.items() if v not in ("", None)}
            try:
                info = await _validate(self.hass, data)
            except HikBridgeAuthError:
                errors["base"] = "invalid_auth"
            except HikBridgeError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                serial = info.get("serial") or f"{data[CONF_HOST]}:{data[CONF_PORT]}"
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"Hikvision PTZ ({serial})", data=data)

        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        return HikBridgeOptionsFlow(entry)


class HikBridgeOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        current = {**self.entry.data, **self.entry.options}
        if user_input is not None:
            data = {k: v for k, v in user_input.items() if v not in ("", None)}
            try:
                await _validate(self.hass, data)
            except HikBridgeAuthError:
                errors["base"] = "invalid_auth"
            except HikBridgeError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="", data=data)
        return self.async_show_form(
            step_id="init", data_schema=_schema(current), errors=errors
        )
