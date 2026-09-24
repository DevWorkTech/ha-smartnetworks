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
from homeassistant.helpers.network import get_url
from homeassistant.helpers import issue_registry as ir

from .api import UmnyeSetiApi
from .payment import build_payment_bridge_path
from .const import (
    DOMAIN,
    DEFAULT_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    DEFAULT_VERIFY_SSL,
    CONF_LOGIN,
    CONF_PASSWORD,
    CONF_VERIFY_SSL,
    CONF_UPDATE_INTERVAL,
    CONF_PAYMENT_LINK_TOKEN,
    INIT_URL)

_LOGGER = logging.getLogger(__name__)

@dataclass
class UmnyeSetiState:
    data: dict | None
    error: str | None
    last_attempt: str | None
    error_details: dict | None = None

class UmnyeSetiCoordinator(DataUpdateCoordinator[UmnyeSetiState]):
    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self._version: str = str(config.get("version") or "0.0.0")
        self._login: str = config[CONF_LOGIN]
        self._password: str = config[CONF_PASSWORD]
        self._verify_ssl: bool = bool(config.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
        self._entry_id: str = config.get("entry_id", "default")
        self._payment_link_token: str = str(config.get(CONF_PAYMENT_LINK_TOKEN) or "")

        interval_min = int(config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL) or DEFAULT_UPDATE_INTERVAL)
        interval_min = max(interval_min, MIN_UPDATE_INTERVAL)

        self._cookie_url = URL(INIT_URL)
        self._cookie_path = hass.config.path(f".storage/umnyeseti_cookies_{self._entry_id}.json")
        self._notification_state_path = hass.config.path(f".storage/umnyeseti_notifications_{self._entry_id}.json")
        self._tariff_notification_state: dict = {
            "last_days": None,
            "notified_threshold": None,
            "persistent_sent": False,
            "mobile_services": [],
            "pending_threshold": None,
            # If the user successfully reached the external payment gateway,
            # suppress re-creating the same warning while end_days is unchanged.
            "payment_dismissed_days": None,
        }

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
        await self._load_notification_state()
        await super().async_config_entry_first_refresh()

    async def async_close(self):
        try:
            await self._save_cookies()
        except Exception:
            pass
        try:
            await self._save_notification_state()
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
            _LOGGER.warning("%s: failed to load cookies: exception_type=%s; exception=%s; path=%s", DOMAIN, type(e).__name__, e, self._cookie_path)

    async def _save_cookies(self):
        try:
            cookies = {k: v.value for k, v in self.session.cookie_jar.filter_cookies(self._cookie_url).items()}
            def _write():
                with open(self._cookie_path, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, ensure_ascii=False, indent=2)
            await self.hass.async_add_executor_job(_write)
        except Exception as e:
            _LOGGER.warning("%s: failed to save cookies: exception_type=%s; exception=%s; path=%s", DOMAIN, type(e).__name__, e, self._cookie_path)

    async def _load_notification_state(self):
        try:
            import os
            if not os.path.exists(self._notification_state_path):
                return

            def _read():
                with open(self._notification_state_path, "r", encoding="utf-8") as f:
                    return json.load(f)

            data = await self.hass.async_add_executor_job(_read)
            if not isinstance(data, dict):
                return

            last_days = data.get("last_days")
            threshold = data.get("notified_threshold")
            services = data.get("mobile_services")
            self._tariff_notification_state = {
                "last_days": int(last_days) if isinstance(last_days, int) else None,
                "notified_threshold": int(threshold) if isinstance(threshold, int) and -3 <= threshold <= 5 else None,
                "persistent_sent": bool(data.get("persistent_sent", False)),
                "mobile_services": [str(x) for x in services if isinstance(x, str)] if isinstance(services, list) else [],
                "pending_threshold": int(data.get("pending_threshold")) if data.get("pending_threshold") in (5, 3, 1, 0, -1, -2, -3) else None,
                "payment_dismissed_days": int(data.get("payment_dismissed_days")) if isinstance(data.get("payment_dismissed_days"), int) else None,
            }
            _LOGGER.debug(
                "%s: tariff notification state loaded: last_days=%s; threshold=%s; mobile_services=%s",
                DOMAIN,
                self._tariff_notification_state.get("last_days"),
                self._tariff_notification_state.get("notified_threshold"),
                self._tariff_notification_state.get("mobile_services"),
            )
        except Exception as e:
            _LOGGER.warning(
                "%s: failed to load tariff notification state: exception_type=%s; exception=%s; path=%s",
                DOMAIN, type(e).__name__, e, self._notification_state_path
            )

    async def _save_notification_state(self):
        try:
            state = {
                "last_days": self._tariff_notification_state.get("last_days"),
                "notified_threshold": self._tariff_notification_state.get("notified_threshold"),
                "persistent_sent": bool(self._tariff_notification_state.get("persistent_sent", False)),
                "mobile_services": sorted(set(self._tariff_notification_state.get("mobile_services") or [])),
                "pending_threshold": self._tariff_notification_state.get("pending_threshold"),
                "payment_dismissed_days": self._tariff_notification_state.get("payment_dismissed_days"),
            }

            def _write():
                with open(self._notification_state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)

            await self.hass.async_add_executor_job(_write)
        except Exception as e:
            _LOGGER.warning(
                "%s: failed to save tariff notification state: exception_type=%s; exception=%s; path=%s",
                DOMAIN, type(e).__name__, e, self._notification_state_path
            )

    def payment_bridge_path(self) -> str | None:
        """Return the token-protected local bridge path."""
        return build_payment_bridge_path(self._entry_id, self._payment_link_token)

    def payment_bridge_url(self) -> str | None:
        """Return an absolute HA bridge URL suitable for device configuration_url."""
        path = self.payment_bridge_path()
        if not path:
            return None
        try:
            base = get_url(
                self.hass,
                allow_internal=True,
                allow_external=True,
                allow_cloud=True,
                prefer_external=True,
            )
        except Exception as exc:
            _LOGGER.debug(
                "%s: cannot build absolute payment bridge URL: exception_type=%s; exception=%s",
                DOMAIN,
                type(exc).__name__,
                exc,
            )
            return None
        return f"{str(base).rstrip('/')}{path}"

    @staticmethod
    def _notification_window_open(now=None) -> bool:
        """Notifications may be sent only from 07:00 inclusive to 22:00 exclusive."""
        current = now or dt_util.now()
        return 7 <= int(current.hour) < 22

    def _tariff_notification_tag(self) -> str:
        return f"umnyeseti_tariff_{self._entry_id}"[:64]

    def _tariff_notification_id(self) -> str:
        return f"umnyeseti_tariff_{self._entry_id}"

    def _mobile_notify_services(self) -> list[str]:
        try:
            services = self.hass.services.async_services().get("notify", {})
            return sorted(name for name in services if str(name).startswith("mobile_app_"))
        except Exception as e:
            _LOGGER.warning(
                "%s: failed to discover mobile notification services: exception_type=%s; exception=%s",
                DOMAIN, type(e).__name__, e
            )
            return []

    def _money_text(self, value) -> str | None:
        """Format money for human-readable notifications, not payment URLs."""
        try:
            if value is None:
                return None
            amount = float(value)
            if amount.is_integer():
                return f"{int(amount)} ₽"
            return f"{amount:.2f}".replace(".", ",") + " ₽"
        except Exception:
            return None

    @staticmethod
    def _notification_end_date(value) -> str | None:
        """Return only the calendar date for the notification body."""
        if value in (None, ""):
            return None
        text = str(value).strip()
        if "," in text:
            text = text.split(",", 1)[0].strip()
        return text or None

    @staticmethod
    def _ru_days_word(days: int) -> str:
        n = abs(int(days))
        last = n % 10
        last2 = n % 100
        if last == 1 and last2 != 11:
            return "день"
        if 2 <= last <= 4 and not 12 <= last2 <= 14:
            return "дня"
        return "дней"

    def _tariff_notification_text(self, mapped: dict, days: int) -> tuple[str, str]:
        ru = self._lang().startswith("ru")
        tariff = mapped.get("tariff") or {}
        end_date = self._notification_end_date(tariff.get("end_subscribe"))
        balance = self._money_text(mapped.get("balance"))
        pay_left = self._money_text(tariff.get("pay_subscribe"))
        account = mapped.get("account")

        if ru:
            if days > 0:
                word = self._ru_days_word(days)
                if days == 1:
                    title = "⚠️ Умные Сети: тариф закончится завтра"
                    intro = "До окончания оплаченного тарифа остался 1 день."
                else:
                    title = f"⚠️ Умные Сети: до окончания тарифа {days} {word}"
                    intro = f"До окончания оплаченного тарифа осталось {days} {word}."
            elif days == 0:
                title = "⚠️ Умные Сети: тариф заканчивается сегодня"
                intro = "Оплаченный тариф заканчивается сегодня."
            else:
                overdue = abs(days)
                word = self._ru_days_word(overdue)
                title = f"⚠️ Умные Сети: тариф просрочен на {overdue} {word}"
                intro = f"Оплаченный тариф просрочен на {overdue} {word}."

            lines = [intro]
            if end_date:
                lines.append(f"Дата окончания: {end_date}")
            if account:
                lines.append(f"Лицевой счёт: {account}")
            if balance:
                lines.append(f"Текущий баланс: {balance}")
            if pay_left:
                lines.append(f"К оплате для продления: {pay_left}")
            return title, "\n".join(lines)

        if days > 0:
            if days == 1:
                title = "⚠️ Smart Networks: plan expires tomorrow"
                intro = "Your paid plan expires tomorrow."
            else:
                title = f"⚠️ Smart Networks: plan expires in {days} days"
                intro = f"Your paid plan expires in {days} days."
        elif days == 0:
            title = "⚠️ Smart Networks: plan expires today"
            intro = "Your paid plan expires today."
        else:
            overdue = abs(days)
            unit = "day" if overdue == 1 else "days"
            title = f"⚠️ Smart Networks: plan overdue by {overdue} {unit}"
            intro = f"Your paid plan is overdue by {overdue} {unit}."

        lines = [intro]
        if end_date:
            lines.append(f"Expiration date: {end_date}")
        if account:
            lines.append(f"Account: {account}")
        if balance:
            lines.append(f"Current balance: {balance}")
        if pay_left:
            lines.append(f"Amount needed to renew: {pay_left}")
        return title, "\n".join(lines)

    async def _async_create_persistent_tariff_notification(
        self, title: str, message: str, payment_url: str | None = None
    ) -> bool:
        try:
            persistent_message = message
            if payment_url:
                label = "Оплатить" if self._lang().startswith("ru") else "Pay now"
                # persistent_notification supports Markdown links. A heading makes
                # the payment action prominent in the Home Assistant notification panel.
                persistent_message = f"### [💳 {label}]({payment_url})\n\n{message}"

            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": title,
                    "message": persistent_message,
                    "notification_id": self._tariff_notification_id(),
                },
                blocking=True,
            )
            return True
        except Exception as e:
            _LOGGER.error(
                "%s: failed to create tariff persistent notification: exception_type=%s; exception=%s",
                DOMAIN, type(e).__name__, e
            )
            return False

    async def _async_dismiss_persistent_tariff_notification(self) -> None:
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": self._tariff_notification_id()},
                blocking=True,
            )
        except Exception as e:
            _LOGGER.warning(
                "%s: failed to dismiss tariff persistent notification: exception_type=%s; exception=%s",
                DOMAIN, type(e).__name__, e
            )

    async def _async_send_mobile_tariff_notification(
        self, service: str, title: str, message: str, payment_url: str | None = None
    ) -> bool:
        try:
            notification_data = {
                "tag": self._tariff_notification_tag(),
                "group": "umnyeseti_tariff",
            }
            if payment_url:
                notification_data["actions"] = [
                    {
                        "action": "URI",
                        "title": "💳 Оплатить" if self._lang().startswith("ru") else "💳 Pay now",
                        "uri": payment_url,
                    }
                ]

            await self.hass.services.async_call(
                "notify",
                service,
                {
                    "title": title,
                    "message": message,
                    "data": notification_data,
                },
                blocking=True,
            )
            return True
        except Exception as e:
            _LOGGER.error(
                "%s: failed to send tariff notification to notify.%s: exception_type=%s; exception=%s",
                DOMAIN, service, type(e).__name__, e
            )
            return False

    async def _async_clear_mobile_tariff_notifications(self, services: list[str]) -> None:
        for service in sorted(set(services)):
            try:
                current = self.hass.services.async_services().get("notify", {})
                if service not in current:
                    continue
                await self.hass.services.async_call(
                    "notify",
                    service,
                    {
                        "message": "clear_notification",
                        "data": {"tag": self._tariff_notification_tag()},
                    },
                    blocking=True,
                )
            except Exception as e:
                _LOGGER.warning(
                    "%s: failed to clear tariff notification on notify.%s: exception_type=%s; exception=%s",
                    DOMAIN, service, type(e).__name__, e
                )

    async def _async_clear_tariff_notifications(self, reason: str) -> None:
        previous_services = list(self._tariff_notification_state.get("mobile_services") or [])
        current_services = self._mobile_notify_services()
        await self._async_dismiss_persistent_tariff_notification()
        await self._async_clear_mobile_tariff_notifications(previous_services + current_services)
        self._tariff_notification_state["notified_threshold"] = None
        self._tariff_notification_state["persistent_sent"] = False
        self._tariff_notification_state["mobile_services"] = []
        self._tariff_notification_state["pending_threshold"] = None
        _LOGGER.info("%s: tariff expiration notifications cleared: reason=%s", DOMAIN, reason)

    async def async_payment_gateway_opened(self) -> None:
        """Dismiss active payment reminders after a confirmed gateway handoff.

        The dismissal is persisted for the currently reported end_days value so
        the same warning is not recreated by the next coordinator refresh. If
        end_days later changes, the normal threshold logic becomes active again.
        """
        days = None
        state = getattr(self, "data", None)
        mapped = getattr(state, "data", None) if state is not None else None
        if isinstance(mapped, dict):
            try:
                raw_days = (mapped.get("tariff") or {}).get("end_days")
                if raw_days is not None:
                    days = int(raw_days)
            except (TypeError, ValueError):
                days = None

        await self._async_clear_tariff_notifications("payment gateway opened successfully")
        self._tariff_notification_state["payment_dismissed_days"] = days
        await self._save_notification_state()
        _LOGGER.info(
            "%s: payment reminder dismissed after gateway handoff: end_days=%s",
            DOMAIN,
            days,
        )

    async def _async_handle_tariff_notifications(self, mapped: dict) -> None:
        tariff = mapped.get("tariff") or {}
        days_raw = tariff.get("end_days")
        if days_raw is None:
            return

        try:
            days = int(days_raw)
        except (TypeError, ValueError):
            _LOGGER.warning("%s: cannot process tariff notification: invalid end_days=%r", DOMAIN, days_raw)
            return

        thresholds = (5, 3, 1, 0, -1, -2, -3)
        old_days = self._tariff_notification_state.get("last_days")
        active_threshold = self._tariff_notification_state.get("notified_threshold")
        dismissed_days = self._tariff_notification_state.get("payment_dismissed_days")
        state_changed = False

        # Payment reminders are meaningful only while the account actually lacks
        # money for the next tariff charge. ``pay_subscribe`` is calculated by
        # the mapper as max(tariff_amount - balance, 0), so a positive value is
        # the single source of truth for "payment is still required".
        try:
            pay_left_raw = tariff.get("pay_subscribe")
            pay_left = float(pay_left_raw) if pay_left_raw is not None else None
        except (TypeError, ValueError):
            pay_left = None

        if pay_left is None or pay_left <= 0:
            has_visible_warning = (
                active_threshold is not None
                or self._tariff_notification_state.get("persistent_sent")
                or self._tariff_notification_state.get("mobile_services")
            )
            if has_visible_warning:
                await self._async_clear_tariff_notifications(
                    "payment reminder no longer needed: balance is sufficient"
                    if pay_left is not None
                    else "payment reminder suppressed: required payment amount is unavailable"
                )
                state_changed = True

            if self._tariff_notification_state.get("pending_threshold") is not None:
                self._tariff_notification_state["pending_threshold"] = None
                state_changed = True
            if self._tariff_notification_state.get("payment_dismissed_days") is not None:
                self._tariff_notification_state["payment_dismissed_days"] = None
                state_changed = True
            if days != old_days:
                self._tariff_notification_state["last_days"] = days
                state_changed = True

            if state_changed:
                await self._save_notification_state()
            _LOGGER.debug(
                "%s: tariff payment reminder suppressed: days=%s; pay_subscribe=%r",
                DOMAIN,
                days,
                pay_left_raw,
            )
            return

        # A successful bridge -> payment gateway handoff dismisses the current
        # warning. Do not recreate it every refresh while the provider still
        # reports the same end_days value. A later day change re-enables the
        # normal threshold schedule (for example 3 days -> 1 day).
        if isinstance(dismissed_days, int):
            if days == dismissed_days:
                if days != old_days:
                    self._tariff_notification_state["last_days"] = days
                    await self._save_notification_state()
                return
            self._tariff_notification_state["payment_dismissed_days"] = None
            state_changed = True

        # Renewal/extension: remove all stale warnings immediately, even during
        # quiet hours. Clearing is not a new notification.
        if isinstance(old_days, int) and days > old_days:
            if (
                active_threshold is not None
                or self._tariff_notification_state.get("persistent_sent")
                or self._tariff_notification_state.get("mobile_services")
            ):
                await self._async_clear_tariff_notifications(
                    f"remaining days increased from {old_days} to {days}"
                )
                state_changed = True
            self._tariff_notification_state["last_days"] = days
            self._tariff_notification_state["pending_threshold"] = None
            await self._save_notification_state()
            return

        if days != old_days:
            self._tariff_notification_state["last_days"] = days
            state_changed = True

        # After the third overdue day there must be no lingering warning.
        if days < -3:
            if (
                active_threshold is not None
                or self._tariff_notification_state.get("persistent_sent")
                or self._tariff_notification_state.get("mobile_services")
            ):
                await self._async_clear_tariff_notifications(
                    f"overdue notification window finished at {days} days"
                )
                state_changed = True
            self._tariff_notification_state["pending_threshold"] = None
            if state_changed:
                await self._save_notification_state()
            return

        pending_threshold = self._tariff_notification_state.get("pending_threshold")

        # Strict quiet hours: never send before 07:00 or at/after 22:00 in
        # Home Assistant's configured local timezone. A threshold reached at
        # night is kept pending across day changes so it cannot be lost.
        if days in thresholds and not self._notification_window_open():
            # Queue only a threshold that has not been delivered yet. If this
            # same threshold was already sent earlier in the day, quiet hours
            # must not turn it into a duplicate morning notification.
            already_delivered = (
                active_threshold == days
                and (
                    self._tariff_notification_state.get("persistent_sent")
                    or self._tariff_notification_state.get("mobile_services")
                )
            )
            if not already_delivered and pending_threshold != days:
                self._tariff_notification_state["pending_threshold"] = days
                state_changed = True
            if state_changed:
                await self._save_notification_state()
            _LOGGER.debug(
                "%s: tariff notification delayed by quiet hours: days=%s; allowed=07:00-22:00; already_delivered=%s",
                DOMAIN,
                days,
                already_delivered,
            )
            return

        if days not in thresholds:
            if pending_threshold is None:
                if state_changed:
                    await self._save_notification_state()
                return
            if not self._notification_window_open():
                if state_changed:
                    await self._save_notification_state()
                return
            # If the integer day counter changed while the notification was
            # sleeping, describe the current state instead of showing stale
            # wording (e.g. 4 days rather than a queued 5-day message).
            notification_days = days if -3 <= days <= 5 else pending_threshold
        else:
            notification_days = days

        title, message = self._tariff_notification_text(mapped, notification_days)
        payment_url = self.payment_bridge_url()
        persistent_payment_url = payment_url

        threshold_changed = active_threshold != notification_days
        if threshold_changed:
            self._tariff_notification_state["notified_threshold"] = notification_days
            self._tariff_notification_state["persistent_sent"] = False
            self._tariff_notification_state["mobile_services"] = []
            active_threshold = days
            state_changed = True
        if self._tariff_notification_state.get("pending_threshold") is not None:
            self._tariff_notification_state["pending_threshold"] = None
            state_changed = True

        if not self._tariff_notification_state.get("persistent_sent", False):
            if await self._async_create_persistent_tariff_notification(title, message, persistent_payment_url):
                self._tariff_notification_state["persistent_sent"] = True
                state_changed = True
                _LOGGER.info(
                    "%s: tariff persistent notification created: days=%s; end=%s",
                    DOMAIN, days, tariff.get("end_subscribe")
                )

        notified_services = set(self._tariff_notification_state.get("mobile_services") or [])
        mobile_services = self._mobile_notify_services()
        if not mobile_services:
            if not notified_services:
                _LOGGER.debug(
                    "%s: tariff warning is active for %s day(s), but no notify.mobile_app_* services are registered",
                    DOMAIN, days
                )
        else:
            for service in mobile_services:
                if service in notified_services:
                    continue
                if await self._async_send_mobile_tariff_notification(service, title, message, payment_url):
                    notified_services.add(service)
                    state_changed = True
                    _LOGGER.info(
                        "%s: tariff expiration push sent: days=%s; service=notify.%s",
                        DOMAIN, days, service
                    )

        self._tariff_notification_state["mobile_services"] = sorted(notified_services)
        if state_changed:
            await self._save_notification_state()

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

    def _raise_issue(self, message: str):
        """Create or update the Home Assistant Repairs issue for this entry."""
        try:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"error_{self._entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="connection_error",
                translation_placeholders={"error": message},
            )
        except Exception:
            # A Repairs problem must never break the data refresh itself.
            _LOGGER.exception("%s: failed to create Home Assistant repair issue", DOMAIN)

    def _clear_issue(self):
        """Remove the Home Assistant Repairs issue after a successful refresh."""
        try:
            ir.async_delete_issue(self.hass, DOMAIN, f"error_{self._entry_id}")
        except Exception:
            # A Repairs problem must never turn a successful refresh into failure.
            _LOGGER.exception("%s: failed to clear Home Assistant repair issue", DOMAIN)

    def _issue_details_text(self, details: dict) -> str:
        ru = self._lang().startswith("ru")
        labels = {
            "stage": "Этап" if ru else "Stage",
            "code": "Код" if ru else "Code",
            "http_status": "HTTP" if ru else "HTTP",
            "url": "URL" if ru else "URL",
            "content_type": "Content-Type" if ru else "Content-Type",
            "location": "Location" if ru else "Location",
            "exception_type": "Тип исключения" if ru else "Exception type",
            "exception": "Исключение" if ru else "Exception",
            "message": "Причина" if ru else "Reason",
            "redirects": "Редиректы" if ru else "Redirects",
            "response_length": "Размер ответа" if ru else "Response size",
            "cookie_names": "Cookies" if ru else "Cookies",
            "payload_keys": "Ключи JSON" if ru else "JSON keys",
            "response_snippet": "Фрагмент ответа" if ru else "Response excerpt",
        }
        lines = []
        for key in ("stage", "code", "http_status", "url", "location", "content_type", "response_length", "exception_type", "exception", "message", "redirects", "cookie_names", "payload_keys", "response_snippet"):
            value = details.get(key)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                value = " -> ".join(str(x) for x in value)
            lines.append(f"{labels[key]}: {value}")
        return "\n".join(lines)

    def _error_state(self, prev, now_utc, fallback_code: str, fallback_stage: str, fallback_message: str = "") -> UmnyeSetiState:
        details = self.api.last_error_details or {
            "code": fallback_code,
            "stage": fallback_stage,
            "message": fallback_message or fallback_code,
        }
        details.setdefault("code", fallback_code)
        details.setdefault("stage", fallback_stage)
        if fallback_message:
            details.setdefault("message", fallback_message)

        error = f"{details.get('code')}: {details.get('message') or fallback_message or details.get('code')}"
        _LOGGER.error("%s update failed: %s", DOMAIN, self.api.format_error_details(details))
        self._raise_issue(self._issue_details_text(details))
        return UmnyeSetiState(data=prev, error=error, last_attempt=now_utc.isoformat(), error_details=details)

    async def _async_update_data(self) -> UmnyeSetiState:
        now_utc = dt_util.utcnow()
        prev = self.data.data if self.data else None

        try:
            j = await self.api.fetch_json()
        except Exception as e:
            _LOGGER.exception("%s: unhandled exception during initial data fetch", DOMAIN)
            details = {"code": "fetch_exception", "stage": "data.fetch", "message": str(e) or type(e).__name__, "exception_type": type(e).__name__}
            self._raise_issue(self._issue_details_text(details))
            return UmnyeSetiState(data=prev, error=f"fetch_exception: {e}", last_attempt=now_utc.isoformat(), error_details=details)

        # Reauthenticate only when the server explicitly indicates an unauthorised
        # session. invalid_json is a response/data problem and must not be masked
        # by a second login attempt.
        if isinstance(j, dict) and j.get("error") == "unauthorized":
            _LOGGER.info("%s: session requires reauthorization; reason=%s", DOMAIN, j.get("error"))
            try:
                auth_resp = await self.api.auth(self._login, self._password)
            except Exception as e:
                _LOGGER.exception("%s: unhandled exception during reauthorization", DOMAIN)
                details = {"code": "auth_exception", "stage": "auth", "message": str(e) or type(e).__name__, "exception_type": type(e).__name__}
                self._raise_issue(self._issue_details_text(details))
                return UmnyeSetiState(data=prev, error=f"auth_exception: {e}", last_attempt=now_utc.isoformat(), error_details=details)

            if not auth_resp or (isinstance(auth_resp, dict) and auth_resp.get("error")):
                msg = (auth_resp or {}).get("message") if isinstance(auth_resp, dict) else ""
                return self._error_state(prev, now_utc, "auth_failed", "auth", msg or "Authorization failed")

            # auth() now proves the session by fetching the JSON endpoint. Reuse
            # that response to avoid an unnecessary third request. Keep the
            # fallback for compatibility with older API implementations.
            verified = auth_resp.get("verified_response") if isinstance(auth_resp, dict) else None
            if isinstance(verified, dict) and not verified.get("error"):
                j = verified
            else:
                try:
                    j = await self.api.fetch_json()
                except Exception as e:
                    _LOGGER.exception("%s: unhandled exception fetching data after authorization", DOMAIN)
                    details = {"code": "fetch_after_auth_exception", "stage": "data.fetch_after_auth", "message": str(e) or type(e).__name__, "exception_type": type(e).__name__}
                    self._raise_issue(self._issue_details_text(details))
                    return UmnyeSetiState(data=prev, error=f"fetch_after_auth_exception: {e}", last_attempt=now_utc.isoformat(), error_details=details)

        if not isinstance(j, dict):
            details = {"code": "invalid_response", "stage": "data.parse", "message": f"Expected dict, got {type(j).__name__}"}
            _LOGGER.error("%s update failed: %s", DOMAIN, details)
            self._raise_issue(self._issue_details_text(details))
            return UmnyeSetiState(data=prev, error="invalid_response", last_attempt=now_utc.isoformat(), error_details=details)

        if j.get("error"):
            msg = str(j.get("message") or j.get("error"))
            return self._error_state(prev, now_utc, str(j.get("error")), "data.fetch", msg)

        raw = j.get("data")
        if raw is None:
            details = {"code": "no_data", "stage": "data.payload", "message": "JSON response does not contain the data field"}
            _LOGGER.error("%s update failed: %s", DOMAIN, details)
            self._raise_issue(self._issue_details_text(details))
            return UmnyeSetiState(data=prev, error="no_data", last_attempt=now_utc.isoformat(), error_details=details)

        if not isinstance(raw, dict):
            details = {"code": "invalid_data_type", "stage": "data.payload", "message": f"Expected data object, got {type(raw).__name__}"}
            _LOGGER.error("%s update failed: %s", DOMAIN, details)
            self._raise_issue(self._issue_details_text(details))
            return UmnyeSetiState(data=prev, error="invalid_data_type", last_attempt=now_utc.isoformat(), error_details=details)

        try:
            mapped = self._map_payload(raw)
        except Exception as e:
            _LOGGER.exception("%s: failed to map account payload; top_level_keys=%s", DOMAIN, sorted(raw.keys()))
            details = {
                "code": "payload_mapping_failed",
                "stage": "data.map",
                "message": str(e) or type(e).__name__,
                "exception_type": type(e).__name__,
                "payload_keys": sorted(raw.keys()),
            }
            self._raise_issue(self._issue_details_text(details))
            return UmnyeSetiState(data=prev, error=f"payload_mapping_failed: {e}", last_attempt=now_utc.isoformat(), error_details=details)

        self._clear_issue()
        try:
            await self._async_handle_tariff_notifications(mapped)
        except Exception:
            # Notification delivery must never make account sensors unavailable.
            _LOGGER.exception("%s: unhandled exception while processing tariff expiration notifications", DOMAIN)
        _LOGGER.debug("%s: account data updated successfully", DOMAIN)
        return UmnyeSetiState(data=mapped, error=None, last_attempt=now_utc.isoformat(), error_details=None)

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
        activities = data.get("activities") or []
        # ISO timestamps sort lexicographically, so keep the newest operation
        # first. This makes "Последний платёж" deterministic even if the
        # provider changes the order of the activities array.
        activities = sorted(
            (p for p in activities if isinstance(p, dict)),
            key=lambda p: str(p.get("d_oper") or ""),
            reverse=True,
        )
        for p in activities:
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
