from __future__ import annotations
import logging
import secrets

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
    CONF_PAYMENT_LINK_TOKEN,
    DEFAULT_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    DEFAULT_VERIFY_SSL,
)
from .api import UmnyeSetiApi

_LOGGER = logging.getLogger(__name__)

def _mask_login(login: str) -> str:
    value = str(login or "")
    if len(value) <= 2:
        return "*" * len(value)
    return f"{value[:1]}***{value[-1:]}"

def _config_error_reason(api: UmnyeSetiApi, fallback: str) -> str:
    details = api.last_error_details
    if not details:
        return fallback
    stage = details.get("stage")
    code = details.get("code")
    status = details.get("http_status")
    message = details.get("message") or fallback
    parts = [str(message)]
    technical = " / ".join(str(x) for x in (stage, code) if x)
    if technical:
        parts.append(f"[{technical}]")
    if status is not None:
        parts.append(f"HTTP {status}")
    return " ".join(parts)

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
            vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
            vol.Optional(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): vol.All(int, vol.Range(min=MIN_UPDATE_INTERVAL)),
        })

        if user_input is not None:
            ui = dict(user_input)
            ui[CONF_UPDATE_INTERVAL] = max(_coerce_int(ui.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL), DEFAULT_UPDATE_INTERVAL), MIN_UPDATE_INTERVAL)

            try:
                session = async_create_clientsession(self.hass, verify_ssl=ui.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
                api = UmnyeSetiApi(session, verify_ssl=ui.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
                auth_resp = await api.auth(ui[CONF_LOGIN], ui[CONF_PASSWORD])
            except Exception as exc:
                _LOGGER.exception(
                    "Umnye Seti config flow: unhandled authorization exception; login=%s; exception_type=%s",
                    _mask_login(ui.get(CONF_LOGIN, "")),
                    type(exc).__name__,
                )
                errors["base"] = "cannot_connect"
                placeholders = {"reason": f"{type(exc).__name__}: {exc}"}
                return self.async_show_form(
                    step_id="user",
                    data_schema=schema,
                    errors=errors,
                    description_placeholders=placeholders,
                )

            if not auth_resp or (isinstance(auth_resp, dict) and auth_resp.get("error")):
                error_code = auth_resp.get("error") if isinstance(auth_resp, dict) else "auth_failed"
                fallback = (auth_resp or {}).get("message") if isinstance(auth_resp, dict) else ""
                if not fallback:
                    fallback = "Неверный логин или пароль"
                reason = _config_error_reason(api, fallback)
                errors["base"] = "cannot_connect" if error_code == "cannot_connect" else "auth_failed"
                placeholders = {"reason": reason}
                _LOGGER.error(
                    "Umnye Seti config flow authorization failed; login=%s; %s",
                    _mask_login(ui.get(CONF_LOGIN, "")),
                    api.format_error_details(api.last_error_details) or f"code={error_code}; message={fallback}",
                )
                return self.async_show_form(
                    step_id="user",
                    data_schema=schema,
                    errors=errors,
                    description_placeholders=placeholders,
                )

            ui[CONF_PAYMENT_LINK_TOKEN] = secrets.token_urlsafe(32)
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
            current = self.config_entry.options.get(
                CONF_UPDATE_INTERVAL,
                self.config_entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
            )
            ui[CONF_UPDATE_INTERVAL] = max(_coerce_int(ui.get(CONF_UPDATE_INTERVAL, current), current), MIN_UPDATE_INTERVAL)
            ui[CONF_VERIFY_SSL] = bool(ui.get(CONF_VERIFY_SSL, self.config_entry.options.get(CONF_VERIFY_SSL, self.config_entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))))
            return self.async_create_entry(title="Options", data=ui)

        opts = self.config_entry.options or {}
        schema = vol.Schema({
            vol.Optional(CONF_UPDATE_INTERVAL, default=_coerce_int(
                opts.get(
                    CONF_UPDATE_INTERVAL,
                    self.config_entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ),
                DEFAULT_UPDATE_INTERVAL,
            )): vol.All(int, vol.Range(min=MIN_UPDATE_INTERVAL)),
            vol.Optional(CONF_VERIFY_SSL, default=bool(opts.get(CONF_VERIFY_SSL, self.config_entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)))): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
