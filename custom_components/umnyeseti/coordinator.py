from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from aiohttp import ClientSession
from yarl import URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.helpers import issue_registry as ir

from .api import UmnyeSetiApi
from .const import (
    DOMAIN,
    DEFAULT_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    CONF_LOGIN,
    CONF_PASSWORD,
    CONF_VERIFY_SSL,
    CONF_UPDATE_INTERVAL,
    INIT_URL)

_LOGGER = logging.getLogger(__name__)

@dataclass
class UmnyeSetiState:
    data: dict | None
    error: str | None
    last_attempt: str | None

class UmnyeSetiCoordinator(DataUpdateCoordinator[UmnyeSetiState]):
    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self._version: str = str(config.get("version") or "0.0.0")
        self._login: str = config[CONF_LOGIN]
        self._password: str = config[CONF_PASSWORD]
        self._verify_ssl: bool = config.get(CONF_VERIFY_SSL, True)
        self._entry_id: str = config.get("entry_id", "default")

        interval_min = int(config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL) or DEFAULT_UPDATE_INTERVAL)
        interval_min = max(interval_min, MIN_UPDATE_INTERVAL)

        self._cookie_url = URL(INIT_URL)
        self._cookie_path = hass.config.path(f".storage/umnyeseti_cookies_{self._entry_id}.json")

        session: ClientSession = async_create_clientsession(hass, verify_ssl=self._verify_ssl)
        self.session = session

        async def persist():
            await self._save_cookies()

        self.api = UmnyeSetiApi(session, verify_ssl=self._verify_ssl, on_cookies=persist, version=self._version)

        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=interval_min))

    async def async_config_entry_first_refresh(self) -> None:
        await self._load_cookies()
        await super().async_config_entry_first_refresh()

    async def async_close(self):
        try:
            await self._save_cookies()
        except Exception:
            pass

    async def _load_cookies(self):
        try:
            import os
            if not os.path.exists(self._cookie_path):
                return
            def _read():
                with open(self._cookie_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            data = await self.hass.async_add_executor_job(_read)
            if isinstance(data, dict) and data:
                self.session.cookie_jar.update_cookies(data, response_url=self._cookie_url)
        except Exception as e:
            _LOGGER.debug("%s: failed to load cookies: %s", DOMAIN, e)

    async def _save_cookies(self):
        try:
            cookies = {k: v.value for k, v in self.session.cookie_jar.filter_cookies(self._cookie_url).items()}
            def _write():
                with open(self._cookie_path, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, ensure_ascii=False, indent=2)
            await self.hass.async_add_executor_job(_write)
        except Exception as e:
            _LOGGER.debug("%s: failed to save cookies: %s", DOMAIN, e)

    def _raise_issue(self, message: str):
        try:
            ir.async_create_issue(
                self.hass, DOMAIN, f"error_{self._entry_id}",
                is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key="connection_error",
                translation_placeholders={"error": message})
        except Exception:
            pass

    def _clear_issue(self):
        try:
            ir.async_delete_issue(self.hass, DOMAIN, f"error_{self._entry_id}")
        except Exception:
            pass

    def _lang(self) -> str:
        lang = getattr(self.hass.config, "language", None) or "en"
        return str(lang).lower()

    def _localize_unknown(self) -> str:
        return "Неизвестно" if self._lang().startswith("ru") else "Unknown"

    def _localize_period(self, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        r = str(raw).lower()
        if self._lang().startswith("ru"):
            return "год" if r == "y" else ("месяц" if r == "m" else r)
        else:
            return "year" if r == "y" else ("month" if r == "m" else r)

    def _localize_speed_unit(self, unit: Optional[str]) -> Optional[str]:
        if not unit:
            return None
        u = str(unit).lower()
        if self._lang().startswith("ru"):
            return "Мбит/с" if u in ("мбит/с", "mbps", "мбитс") else unit
        return "Mbps" if u in ("мбит/с", "mbps", "мбитс") else unit

    def _money(self, val: Optional[float]) -> Optional[float]:
        if val is None:
            return None
        try:
            return round(float(val), 2)
        except Exception:
            return None

    def _normalize_mac(self, s: Optional[str]) -> Optional[str]:
        if not s:
            return None
        s = s.strip().replace("-", ":").replace(" ", "")
        return s.lower()

    def _to_human(self, iso: Optional[str]) -> Optional[str]:
        if not iso:
            return None
        try:
            dt = dt_util.parse_datetime(iso)
            if not dt:
                return None
            dt = dt_util.as_local(dt)
            return dt.strftime("%d.%m.%Y, %H:%M")
        except Exception:
            return None

    def _days_until(self, iso: Optional[str]) -> Optional[int]:
        try:
            if not iso:
                return None
            dt = dt_util.parse_datetime(iso)
            if not dt:
                return None
            local = dt_util.as_local(dt)
            now_local = dt_util.as_local(dt_util.utcnow())
            d1 = now_local.date()
            d2 = local.date()
            return (d2 - d1).days
        except Exception:
            return None

    async def _async_update_data(self) -> UmnyeSetiState:
        now_utc = dt_util.utcnow()
        prev = self.data.data if self.data else None

        try:
            j = await self.api.fetch_json()
        except Exception as e:
            self._raise_issue(f"fetch_exception: {e}")
            return UmnyeSetiState(data=prev, error=f"fetch_exception: {e}", last_attempt=now_utc.isoformat())

        if isinstance(j, dict) and j.get("error") in ("unauthorized", "invalid_json"):
            try:
                auth_resp = await self.api.auth(self._login, self._password)
            except Exception as e:
                self._raise_issue(f"auth_exception: {e}")
                return UmnyeSetiState(data=prev, error=f"auth_exception: {e}", last_attempt=now_utc.isoformat())

            if not auth_resp or (isinstance(auth_resp, dict) and auth_resp.get("error")):
                err = auth_resp.get("error") if isinstance(auth_resp, dict) else "auth_failed"
                msg = (auth_resp or {}).get("message") if isinstance(auth_resp, dict) else ""
                self._raise_issue(f"auth_failed: {msg or err}")
                return UmnyeSetiState(data=prev, error=f"auth_failed: {msg or err}", last_attempt=now_utc.isoformat())

            try:
                j = await self.api.fetch_json()
            except Exception as e:
                self._raise_issue(f"fetch_after_auth_exception: {e}")
                return UmnyeSetiState(data=prev, error=f"fetch_after_auth_exception: {e}", last_attempt=now_utc.isoformat())

        if not isinstance(j, dict):
            self._raise_issue("invalid_response")
            return UmnyeSetiState(data=prev, error="invalid_response", last_attempt=now_utc.isoformat())

        if j.get("error"):
            err = j.get("error")
            self._raise_issue(f"errors: {err}")
            return UmnyeSetiState(data=prev, error=f"errors: {err}", last_attempt=now_utc.isoformat())

        raw = j.get("data")
        if raw is None:
            self._raise_issue("no_data")
            return UmnyeSetiState(data=prev, error="no_data", last_attempt=now_utc.isoformat())

        self._clear_issue()
        return UmnyeSetiState(data=self._map_payload(raw), error=None, last_attempt=now_utc.isoformat())

    def _map_payload(self, data: dict) -> dict:
        vlanID = "0"
        ip = "0.0.0.0"
        mac = "00:00:00:00:00:00"
        address = self._localize_unknown()

        for addr in data.get("equipment_addresses", []):
            t = str(addr.get("n_addr_type_id"))
            if t == "5006":
                vlanID = addr.get("vc_code", vlanID)
            elif t == "4006":
                mac = self._normalize_mac(addr.get("vc_code")) or mac
            elif t == "3006":
                ip = addr.get("vc_code", ip)
            elif t == "1006":
                address = addr.get("vc_code", address) or self._localize_unknown()

        pays = []
        for p in data.get("activities", []):
            iso = p.get("d_oper")
            amount = self._money(p.get("n_value_1"))
            human = self._to_human(iso)
            pays.append({"date": human, "amount": amount})

        serv0 = (data.get("servs") or [{}])[0]
        tariff_info = serv0.get("detailed_info", {}) or {}
        speed_val = tariff_info.get("n_speed_volume_cur")
        speed_unit = self._localize_speed_unit(tariff_info.get("vc_speed_unit_cur"))
        period_text = self._localize_period(serv0.get("c_period"))
        end_iso = serv0.get("d_charge_log_end")
        end_human = self._to_human(end_iso)
        end_days = self._days_until(end_iso)

        balance = self._money((data.get("personal_accounts") or [{}])[0].get("n_sum_bal"))
        tariff_amount = self._money(serv0.get("n_good_base_sum"))
        pay_left = None
        if tariff_amount is not None and balance is not None:
            pay_left = max(round(tariff_amount - balance, 2), 0.0)

        mapped = {
            "account": (data.get("personal_accounts") or [{}])[0].get("vc_account"),
            "balance": balance,
            "subscriber": (data.get("person") or {}).get("vc_name"),
            "address": address,
            "net": {"ip": ip, "mac": mac, "vlan": vlanID},
            "tariff": {
                "name": serv0.get("vc_name"),
                "speed": f"{speed_val} {speed_unit}".strip() if speed_val is not None else None,
                "amount": tariff_amount,
                "period": period_text,
                "end_subscribe": end_human,
                "end_days": end_days,
                "pay_subscribe": pay_left,
            },
            "pays": pays,
        }
        return mapped
