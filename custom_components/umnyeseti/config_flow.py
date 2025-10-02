from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    DOMAIN,
    CONF_LOGIN,
    CONF_PASSWORD,
    CONF_VERIFY_SSL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)
from .api import UmnyeSetiApi

def _coerce_int(v, default):
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return default

class UmnyeSetiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        placeholders = {}
        schema = vol.Schema({
            vol.Required(CONF_LOGIN): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_VERIFY_SSL, default=True): bool,
            vol.Optional(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): vol.All(int, vol.Range(min=MIN_UPDATE_INTERVAL)),
        })

        if user_input is not None:
            ui = dict(user_input)
            ui[CONF_UPDATE_INTERVAL] = max(_coerce_int(ui.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL), DEFAULT_UPDATE_INTERVAL), MIN_UPDATE_INTERVAL)

            try:
                session = async_create_clientsession(self.hass, verify_ssl=ui.get(CONF_VERIFY_SSL, True))
                api = UmnyeSetiApi(session, verify_ssl=ui.get(CONF_VERIFY_SSL, True))
                auth_resp = await api.auth(ui[CONF_LOGIN], ui[CONF_PASSWORD])
            except Exception:
                errors["base"] = "cannot_connect"
                return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

            if not auth_resp or (isinstance(auth_resp, dict) and auth_resp.get("error") in ("auth_failed", "unauthorized")):
                errors["base"] = "auth_failed"
                reason = (auth_resp or {}).get("message") if isinstance(auth_resp, dict) else ""
                if not reason:
                    reason = "Неверный логин или пароль"
                placeholders = {"reason": reason}
                return self.async_show_form(step_id="user", data_schema=schema, errors=errors, description_placeholders=placeholders)

            await self.async_set_unique_id(f"login:{ui[CONF_LOGIN]}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"Умные Сети ({ui[CONF_LOGIN]})", data=ui)

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors, description_placeholders=placeholders)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return UmnyeSetiOptionsFlow(config_entry)

class UmnyeSetiOptionsFlow(config_entries.OptionsFlowWithConfigEntry):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            ui = dict(user_input)
            current = self.config_entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            ui[CONF_UPDATE_INTERVAL] = max(_coerce_int(ui.get(CONF_UPDATE_INTERVAL, current), current), MIN_UPDATE_INTERVAL)
            ui[CONF_VERIFY_SSL] = bool(ui.get(CONF_VERIFY_SSL, self.config_entry.options.get(CONF_VERIFY_SSL, True)))
            return self.async_create_entry(title="Options", data=ui)

        opts = self.config_entry.options or {}
        schema = vol.Schema({
            vol.Optional(CONF_UPDATE_INTERVAL, default=_coerce_int(opts.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL), DEFAULT_UPDATE_INTERVAL)): vol.All(int, vol.Range(min=MIN_UPDATE_INTERVAL)),
            vol.Optional(CONF_VERIFY_SSL, default=bool(opts.get(CONF_VERIFY_SSL, True))): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
