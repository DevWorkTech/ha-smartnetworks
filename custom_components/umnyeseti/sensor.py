from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Optional, List

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_LOGIN
from .coordinator import UmnyeSetiCoordinator


SENSOR_NAME = {
    "status": "Статус",
    "account": "Лицевой счёт",
    "balance": "Баланс",
    "subscriber": "Абонент",
    "address": "Адрес",
    "ip": "IP-адрес",
    "mac": "MAC-адрес",
    "vlan": "VLAN",
    "tariff_name": "Тариф",
    "tariff_speed": "Скорость",
    "tariff_amount": "Стоимость тарифа",
    "tariff_period": "Период списаний",
    "tariff_end": "Окончание подписки",
    "tariff_pay_left": "К оплате",
    "pays": "Платежи",
    "last_update": "Последнее обновление",
}


ICON = {
    "status": "mdi:information-outline",
    "account": "mdi:account-card",
    "balance": "mdi:currency-rub",
    "subscriber": "mdi:account",
    "address": "mdi:home",
    "ip": "mdi:ip-network",
    "mac": "mdi:lan",
    "vlan": "mdi:lan",
    "tariff_name": "mdi:tag",
    "tariff_speed": "mdi:speedometer",
    "tariff_amount": "mdi:currency-rub",
    "tariff_period": "mdi:calendar",
    "tariff_end": "mdi:calendar-end",
    "tariff_pay_left": "mdi:cash-clock",
    "pays": "mdi:receipt",
    "last_update": "mdi:clock-outline",
}

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    coordinator: UmnyeSetiCoordinator = hass.data[DOMAIN][entry.entry_id]

    # v1.0.20 briefly exposed RAW HTTP diagnostics as a sensor. RAW capture is
    # now always available through Home Assistant's standard "Download
    # diagnostics" action, so remove the obsolete entity from the registry on
    # upgrade instead of leaving an unavailable RAW sensor behind.
    from homeassistant.helpers import entity_registry as er

    login = entry.data.get(CONF_LOGIN)
    raw_unique_id = f"umnyeseti_{login}_raw"
    registry = er.async_get(hass)
    raw_entity_id = registry.async_get_entity_id("sensor", DOMAIN, raw_unique_id)
    if raw_entity_id:
        registry.async_remove(raw_entity_id)

    entities: List[SensorEntity] = [
        StatusSensor(coordinator, entry),
        SimpleValueSensor(coordinator, entry, "account", "account"),
        MoneyValueSensor(coordinator, entry, "balance", "balance"),
        SimpleValueSensor(coordinator, entry, "subscriber", "subscriber"),
        SimpleValueSensor(coordinator, entry, "address", "address"),
        NestedValueSensor(coordinator, entry, "ip", ["net", "ip"], "ip"),
        NestedValueSensor(coordinator, entry, "mac", ["net", "mac"], "mac"),
        NestedValueSensor(coordinator, entry, "vlan", ["net", "vlan"], "vlan"),
        NestedValueSensor(coordinator, entry, "tariff_name", ["tariff", "name"], "tariff_name"),
        NestedValueSensor(coordinator, entry, "tariff_speed", ["tariff", "speed"], "tariff_speed"),
        MoneyNestedSensor(coordinator, entry, "tariff_amount", ["tariff", "amount"], "tariff_amount"),
        NestedValueSensor(coordinator, entry, "tariff_period", ["tariff", "period"], "tariff_period"),
        TariffEndSensor(coordinator, entry),
        TariffPayLeftSensor(coordinator, entry),
        PaymentsSensor(coordinator, entry),
        LastUpdateSensor(coordinator, entry),
    ]
    add_entities(entities)

class BaseUmnyeSetiSensor(CoordinatorEntity[UmnyeSetiCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: UmnyeSetiCoordinator, entry: ConfigEntry, key: str, name_suffix: Optional[str] = None):
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        login = entry.data.get(CONF_LOGIN)
        suffix = name_suffix or key
        self._attr_unique_id = f"umnyeseti_{login}_{suffix}"
        # Keep the original human-friendly Russian names stable regardless of
        # Home Assistant backend language or translation cache. unique_id is
        # intentionally unchanged, so existing history and automations remain.
        self._attr_name = SENSOR_NAME.get(suffix, suffix)
        icon = ICON.get(suffix)
        if icon:
            self._attr_icon = icon
        # Mark diagnostic sensors
        if suffix in ('ip', 'mac', 'vlan'):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _payment_attributes(self) -> dict[str, Any]:
        """Return the payment action first in every entity card."""
        payment_url = self.coordinator.payment_bridge_url() or self.coordinator.payment_bridge_path()
        return {"💳 Оплатить": payment_url} if payment_url else {}

    @property
    def extra_state_attributes(self):
        # Keep the payment action visible at the top of every more-info card.
        return self._payment_attributes() or None

    @property
    def device_info(self):
        login = self._entry.data.get(CONF_LOGIN)
        return {
            "identifiers": {(DOMAIN, f"account:{login}")},
            "name": f"Умные Сети ({login})",
            "manufacturer": "Smart Networks",
            "model": "Личный кабинет абонента",
            "sw_version": getattr(self.coordinator, "_version", "0.0.0"),
            "suggested_area": "Internet",
            "configuration_url": self.coordinator.payment_bridge_url() or "https://stat.umnyeseti.ru",
        }

class StatusSensor(BaseUmnyeSetiSensor):
    def __init__(self, coordinator: UmnyeSetiCoordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry, "status", "status")

    @property
    def native_value(self):
        st = self.coordinator.data
        if not st:
            return "ERROR"
        return "ERROR" if st.error else "OK"

    @property
    def extra_state_attributes(self):
        st = self.coordinator.data
        if not st:
            return {"error": "no_state"}

        attrs = self._payment_attributes()
        attrs.update({
            "error": st.error,
            "last_attempt": st.last_attempt,
        })
        details = getattr(st, "error_details", None) or {}
        for source, target in (
            ("code", "error_code"),
            ("stage", "error_stage"),
            ("http_status", "http_status"),
            ("url", "request_url"),
            ("location", "redirect_location"),
            ("content_type", "content_type"),
            ("response_length", "response_length"),
            ("message", "error_reason"),
            ("exception_type", "exception_type"),
            ("payload_keys", "payload_keys"),
        ):
            value = details.get(source)
            if value not in (None, "", [], {}):
                attrs[target] = value
        redirects = details.get("redirects")
        if redirects:
            attrs["redirects"] = redirects
        return attrs

class SimpleValueSensor(BaseUmnyeSetiSensor):
    def __init__(self, coordinator: UmnyeSetiCoordinator, entry: ConfigEntry, key: str, field: str):
        super().__init__(coordinator, entry, key)
        self._field = field

    @property
    def native_value(self):
        st = self.coordinator.data
        if not st or not st.data:
            return None
        return st.data.get(self._field)

class MoneyValueSensor(SimpleValueSensor):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = 'RUB'
    _attr_suggested_display_precision = 2

class NestedValueSensor(BaseUmnyeSetiSensor):
    def __init__(self, coordinator: UmnyeSetiCoordinator, entry: ConfigEntry, key: str, path: list[str], name_suffix: str):
        super().__init__(coordinator, entry, key, name_suffix)
        self._path = path

    @property
    def native_value(self):
        st = self.coordinator.data
        if not st or not st.data:
            return None
        node: Any = st.data
        for p in self._path:
            node = node.get(p) if isinstance(node, dict) else None
            if node is None:
                return None
        return node

class MoneyNestedSensor(NestedValueSensor):
    _attr_native_unit_of_measurement = "₽"
    _attr_suggested_display_precision = 2

class TariffEndSensor(BaseUmnyeSetiSensor):
    def __init__(self, coordinator: UmnyeSetiCoordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry, "tariff_end", "tariff_end")

    def _lang(self) -> str:
        lang = getattr(self.hass.config, "language", None) or "en"
        return str(lang).lower()

    def _plural_days_ru(self, n: int) -> str:
        last = n % 10
        last2 = n % 100
        if last == 1 and last2 != 11:
            suffix = "день"
        elif 2 <= last <= 4 and not 12 <= last2 <= 14:
            suffix = "дня"
        else:
            suffix = "дней"
        return f"{n} {suffix}"

    @property
    def native_value(self):
        st = self.coordinator.data
        if not st or not st.data:
            return None
        t = st.data.get("tariff") or {}
        days = t.get("end_days")
        if days is None:
            return None
        days = int(days)
        if days < 0:
            return "просрочено" if self._lang().startswith("ru") else "overdue"
        if days == 0:
            return "сегодня" if self._lang().startswith("ru") else "today"
        if days == 1:
            return "завтра" if self._lang().startswith("ru") else "tomorrow"
        return self._plural_days_ru(days) if self._lang().startswith("ru") else f"{days} days"

    @property
    def extra_state_attributes(self):
        st = self.coordinator.data
        if not st or not st.data:
            return None
        t = st.data.get("tariff") or {}
        attrs = self._payment_attributes()
        attrs["scheduled_end"] = t.get("end_subscribe")
        return attrs

class TariffPayLeftSensor(MoneyNestedSensor):
    """Amount required to renew, with a ready-to-use external payment URL."""

    def __init__(self, coordinator: UmnyeSetiCoordinator, entry: ConfigEntry):
        super().__init__(
            coordinator, entry, "tariff_pay_left", ["tariff", "pay_subscribe"], "tariff_pay_left"
        )

    @property
    def extra_state_attributes(self):
        st = self.coordinator.data
        if not st or not st.data:
            return None
        return self._payment_attributes() or None

class PaymentsSensor(BaseUmnyeSetiSensor):
    """Payment history in a compact, human-readable Home Assistant view."""

    _MAX_VISIBLE_PAYMENTS = 30

    def __init__(self, coordinator: UmnyeSetiCoordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry, "pays", "pays")

    @staticmethod
    def _plural_payments_ru(count: int) -> str:
        last = count % 10
        last2 = count % 100
        if last == 1 and last2 != 11:
            word = "платёж"
        elif 2 <= last <= 4 and not 12 <= last2 <= 14:
            word = "платежа"
        else:
            word = "платежей"
        return f"{count} {word}"

    @staticmethod
    def _format_amount(amount: Any) -> str:
        """Format money without unnecessary .00 and with readable groups."""
        if amount is None:
            return "—"
        try:
            value = float(amount)
        except (TypeError, ValueError):
            return f"{amount} ₽"

        if value.is_integer():
            return f"{int(value):,} ₽".replace(",", " ")

        text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
        return f"{text} ₽"

    @classmethod
    def _format_payment(cls, payment: dict) -> str:
        date = payment.get("date") or "дата неизвестна"
        return f"{date} — {cls._format_amount(payment.get('amount'))}"

    @property
    def native_value(self):
        pays = self._get_pays()
        if pays is None:
            return None
        return self._plural_payments_ru(len(pays))

    @property
    def extra_state_attributes(self):
        pays = self._get_pays()
        if pays is None:
            return None

        attrs: dict[str, Any] = self._payment_attributes()
        attrs["Количество платежей"] = len(pays)
        visible = pays[: self._MAX_VISIBLE_PAYMENTS]

        if visible:
            attrs["Последний платёж"] = self._format_payment(visible[0])

            # Attribute names are rendered on the left and values on the right
            # in Home Assistant's more-info dialog.  Use the payment date as
            # the label so the history looks like a simple date / amount list.
            for payment in visible:
                date = str(payment.get("date") or "Дата неизвестна")
                key = date
                duplicate = 2
                while key in attrs:
                    key = f"{date} ({duplicate})"
                    duplicate += 1
                attrs[key] = self._format_amount(payment.get("amount"))

        if len(pays) > self._MAX_VISIBLE_PAYMENTS:
            attrs["Показано платежей"] = self._MAX_VISIBLE_PAYMENTS

        return attrs

    @property
    def icon(self):
        return "mdi:credit-card-outline"

    def _get_pays(self):
        st = self.coordinator.data
        if not st or not st.data:
            return None
        pays = st.data.get("pays")
        return pays if isinstance(pays, list) else []

class LastUpdateSensor(BaseUmnyeSetiSensor):
    def __init__(self, coordinator: UmnyeSetiCoordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry, "last_update", "last_update")
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._last_attempt: Optional[datetime] = None

    async def async_added_to_hass(self) -> None:
        self._last_attempt = datetime.now(timezone.utc)
        await super().async_added_to_hass()

    def _handle_coordinator_update(self) -> None:
        st = self.coordinator.data
        if st and getattr(st, "last_attempt", None):
            try:
                from homeassistant.util import dt as dt_util
                dt = dt_util.parse_datetime(st.last_attempt)
                if dt is not None:
                    self._last_attempt = dt
            except Exception:
                self._last_attempt = datetime.now(timezone.utc)
        else:
            self._last_attempt = datetime.now(timezone.utc)
        super()._handle_coordinator_update()

    @property
    def native_value(self):
        return self._last_attempt
