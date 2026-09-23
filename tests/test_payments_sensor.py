from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "umnyeseti"


def _install_stubs() -> None:
    modules = {}
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    helpers = types.ModuleType("homeassistant.helpers")
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")
    config_entries = types.ModuleType("homeassistant.config_entries")
    core = types.ModuleType("homeassistant.core")
    entity = types.ModuleType("homeassistant.helpers.entity")
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")

    class SensorEntity:
        pass

    class SensorDeviceClass:
        MONETARY = "monetary"
        TIMESTAMP = "timestamp"

    class CoordinatorEntity:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

    class ConfigEntry:
        pass

    class HomeAssistant:
        pass

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"

    class AddEntitiesCallback:
        pass

    sensor.SensorEntity = SensorEntity
    sensor.SensorDeviceClass = SensorDeviceClass
    uc.CoordinatorEntity = CoordinatorEntity
    config_entries.ConfigEntry = ConfigEntry
    core.HomeAssistant = HomeAssistant
    entity.EntityCategory = EntityCategory
    entity_platform.AddEntitiesCallback = AddEntitiesCallback

    modules.update({
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.sensor": sensor,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.update_coordinator": uc,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.helpers.entity": entity,
        "homeassistant.helpers.entity_platform": entity_platform,
    })
    sys.modules.update(modules)


def _load_sensor_module():
    _install_stubs()
    package = types.ModuleType("custom_components")
    package.__path__ = []
    integration = types.ModuleType("custom_components.umnyeseti")
    integration.__path__ = [str(COMPONENT)]
    sys.modules["custom_components"] = package
    sys.modules["custom_components.umnyeseti"] = integration

    const_spec = importlib.util.spec_from_file_location(
        "custom_components.umnyeseti.const", COMPONENT / "const.py"
    )
    const_mod = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const_mod
    const_spec.loader.exec_module(const_mod)

    coord_mod = types.ModuleType("custom_components.umnyeseti.coordinator")
    coord_mod.UmnyeSetiCoordinator = type("UmnyeSetiCoordinator", (), {})
    sys.modules["custom_components.umnyeseti.coordinator"] = coord_mod

    spec = importlib.util.spec_from_file_location(
        "custom_components.umnyeseti.sensor", COMPONENT / "sensor.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sensor_module = _load_sensor_module()
PaymentsSensor = sensor_module.PaymentsSensor
TariffPayLeftSensor = sensor_module.TariffPayLeftSensor
TariffEndSensor = sensor_module.TariffEndSensor


class Entry:
    data = {"login": "00123456"}


class State:
    def __init__(self, pays, account=None, pay_left=None, end_days=5):
        self.data = {
            "pays": pays,
            "account": account,
            "tariff": {
                "pay_subscribe": pay_left,
                "end_days": end_days,
                "end_subscribe": "31.08.2026, 23:59",
            },
        }


class Api:
    raw_debug = {"data.fetch": {"request": {"method": "GET"}, "response": {"status": 200, "body": "{\"data\":{}}"}}}


class Coordinator:
    def __init__(self, pays, account=None, pay_left=None, end_days=5):
        self.data = State(pays, account, pay_left, end_days)
        self.api = Api()

    def payment_bridge_path(self):
        return "/api/umnyeseti/payment/entry123/token123"

    def payment_bridge_url(self):
        return "https://ha.example/api/umnyeseti/payment/entry123/token123"


class PaymentsSensorTests(unittest.TestCase):
    def make_sensor(self, pays):
        return PaymentsSensor(Coordinator(pays), Entry())

    def test_state_is_payment_count_not_fake_open_button(self):
        sensor = self.make_sensor([
            {"date": "26.08.2026, 12:00", "amount": 600.0},
            {"date": "25.07.2026, 12:00", "amount": 600.0},
        ])
        self.assertEqual(sensor.native_value, "2 платежа")
        self.assertNotEqual(sensor.native_value, "Открыть")

    def test_attributes_expose_date_amount_rows_without_raw_pays(self):
        pays = [
            {"date": "02.11.2025, 14:59", "amount": 10196},
            {"date": "02.10.2025, 09:14", "amount": 1},
            {"date": "25.07.2026, 12:00", "amount": 550.5},
        ]
        sensor = self.make_sensor(pays)
        attrs = sensor.extra_state_attributes
        self.assertEqual(attrs["Количество платежей"], 3)
        self.assertEqual(attrs["Последний платёж"], "02.11.2025, 14:59 — 10 196 ₽")
        self.assertEqual(attrs["02.11.2025, 14:59"], "10 196 ₽")
        self.assertEqual(attrs["02.10.2025, 09:14"], "1 ₽")
        self.assertEqual(attrs["25.07.2026, 12:00"], "550,50 ₽")
        self.assertNotIn("pays", attrs)
        self.assertFalse(any(key.startswith("Платёж ") for key in attrs))

    def test_duplicate_dates_are_not_lost(self):
        sensor = self.make_sensor([
            {"date": "02.10.2025, 09:14", "amount": 1},
            {"date": "02.10.2025, 09:14", "amount": 2},
        ])
        attrs = sensor.extra_state_attributes
        self.assertEqual(attrs["02.10.2025, 09:14"], "1 ₽")
        self.assertEqual(attrs["02.10.2025, 09:14 (2)"], "2 ₽")


    def test_payment_url_is_exposed_in_relevant_cards(self):
        coordinator = Coordinator([], account="00123456", pay_left=1)
        pay_sensor = TariffPayLeftSensor(coordinator, Entry())
        end_sensor = TariffEndSensor(coordinator, Entry())

        expected = "https://ha.example/api/umnyeseti/payment/entry123/token123"
        self.assertEqual(pay_sensor.extra_state_attributes["💳 Оплатить"], expected)
        self.assertEqual(end_sensor.extra_state_attributes["💳 Оплатить"], expected)

        pays_sensor = PaymentsSensor(coordinator, Entry())
        self.assertEqual(pays_sensor.extra_state_attributes["💳 Оплатить"], expected)



    def test_payment_action_is_first_attribute(self):
        coordinator = Coordinator([{"date": "02.11.2025, 14:59", "amount": 10196}], account="00123456", pay_left=450)
        expected_key = "💳 Оплатить"

        pay_sensor = TariffPayLeftSensor(coordinator, Entry())
        end_sensor = TariffEndSensor(coordinator, Entry())
        pays_sensor = PaymentsSensor(coordinator, Entry())

        self.assertEqual(next(iter(pay_sensor.extra_state_attributes)), expected_key)
        self.assertEqual(next(iter(end_sensor.extra_state_attributes)), expected_key)
        self.assertEqual(next(iter(pays_sensor.extra_state_attributes)), expected_key)

    def test_zero_payments(self):
        sensor = self.make_sensor([])
        self.assertEqual(sensor.native_value, "0 платежей")
        self.assertEqual(sensor.extra_state_attributes["Количество платежей"], 0)


if __name__ == "__main__":
    unittest.main()
