from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "umnyeseti"


def _install_homeassistant_stubs() -> None:
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
    network = types.ModuleType("homeassistant.helpers.network")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")

    class HomeAssistant:
        pass

    class DataUpdateCoordinator:
        def __class_getitem__(cls, item):
            return cls

    class IssueSeverity:
        ERROR = "error"

    core.HomeAssistant = HomeAssistant
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    aiohttp_client.async_create_clientsession = lambda *args, **kwargs: None
    issue_registry.IssueSeverity = IssueSeverity
    issue_registry.async_create_issue = lambda *args, **kwargs: None
    issue_registry.async_delete_issue = lambda *args, **kwargs: None
    helpers.issue_registry = issue_registry
    network.get_url = lambda *args, **kwargs: "https://ha.example"
    util.dt = dt

    modules = {
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.update_coordinator": update_coordinator,
        "homeassistant.helpers.aiohttp_client": aiohttp_client,
        "homeassistant.helpers.issue_registry": issue_registry,
        "homeassistant.helpers.network": network,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
    }
    sys.modules.update(modules)


def _load_coordinator_module():
    _install_homeassistant_stubs()

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

    api_mod = types.ModuleType("custom_components.umnyeseti.api")
    api_mod.UmnyeSetiApi = type("UmnyeSetiApi", (), {})
    sys.modules["custom_components.umnyeseti.api"] = api_mod

    spec = importlib.util.spec_from_file_location(
        "custom_components.umnyeseti.coordinator", COMPONENT / "coordinator.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


coordinator_module = _load_coordinator_module()
UmnyeSetiCoordinator = coordinator_module.UmnyeSetiCoordinator


class FakeServices:
    def __init__(self):
        self.calls = []
        self.notify_services = {
            "mobile_app_phone": object(),
            "mobile_app_tablet": object(),
            "some_other_notifier": object(),
        }

    def async_services(self):
        return {
            "notify": self.notify_services,
            "persistent_notification": {"create": object(), "dismiss": object()},
        }

    async def async_call(self, domain, service, data, blocking=False, **kwargs):
        self.calls.append((domain, service, data, blocking))


class FakeConfig:
    language = "ru"


class FakeHass:
    def __init__(self):
        self.services = FakeServices()
        self.config = FakeConfig()


class TariffNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = FakeHass()
        self.coordinator = object.__new__(UmnyeSetiCoordinator)
        self.coordinator.hass = self.hass
        self.coordinator._entry_id = "entry123"
        self.coordinator._payment_link_token = "token123"
        self.coordinator._tariff_notification_state = {
            "last_days": None,
            "notified_threshold": None,
            "persistent_sent": False,
            "mobile_services": [],
            "pending_threshold": None,
            "payment_dismissed_days": None,
        }
        self.coordinator._notification_window_open = lambda now=None: True

        async def save_noop():
            return None

        self.coordinator._save_notification_state = save_noop

    def payload(self, days):
        return {
            "account": "12345678",
            "balance": 150.0,
            "tariff": {
                "name": "Интернет 500",
                "end_subscribe": "31.08.2026, 23:59",
                "end_days": days,
                "pay_subscribe": 450.0,
            },
        }

    def mobile_messages(self):
        return [
            call for call in self.hass.services.calls
            if call[0] == "notify" and call[2].get("message") != "clear_notification"
        ]

    async def test_thresholds_are_sent_once_and_replace_previous_warning(self):
        await self.coordinator._async_handle_tariff_notifications(self.payload(5))
        self.assertEqual(len(self.mobile_messages()), 2)
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], 5)

        first_count = len(self.hass.services.calls)
        await self.coordinator._async_handle_tariff_notifications(self.payload(5))
        self.assertEqual(len(self.hass.services.calls), first_count)

        await self.coordinator._async_handle_tariff_notifications(self.payload(4))
        self.assertEqual(len(self.hass.services.calls), first_count)

        await self.coordinator._async_handle_tariff_notifications(self.payload(3))
        self.assertEqual(len(self.mobile_messages()), 4)
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], 3)

        await self.coordinator._async_handle_tariff_notifications(self.payload(2))
        await self.coordinator._async_handle_tariff_notifications(self.payload(1))
        self.assertEqual(len(self.mobile_messages()), 6)
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], 1)

    async def test_extension_clears_home_assistant_and_phone_notifications(self):
        await self.coordinator._async_handle_tariff_notifications(self.payload(3))
        self.hass.services.calls.clear()

        await self.coordinator._async_handle_tariff_notifications(self.payload(30))

        persistent_dismiss = [
            c for c in self.hass.services.calls
            if c[0] == "persistent_notification" and c[1] == "dismiss"
        ]
        mobile_clear = [
            c for c in self.hass.services.calls
            if c[0] == "notify" and c[2].get("message") == "clear_notification"
        ]
        self.assertEqual(len(persistent_dismiss), 1)
        self.assertEqual(len(mobile_clear), 2)
        self.assertIsNone(self.coordinator._tariff_notification_state["notified_threshold"])
        self.assertFalse(self.coordinator._tariff_notification_state["persistent_sent"])
        self.assertEqual(self.coordinator._tariff_notification_state["mobile_services"], [])
        self.assertEqual(self.coordinator._tariff_notification_state["last_days"], 30)

    async def test_only_mobile_app_notify_services_are_used(self):
        await self.coordinator._async_handle_tariff_notifications(self.payload(5))
        services = {c[1] for c in self.mobile_messages()}
        self.assertEqual(services, {"mobile_app_phone", "mobile_app_tablet"})


    async def test_payment_action_and_persistent_link_use_gateway_url(self):
        await self.coordinator._async_handle_tariff_notifications(self.payload(5))

        mobile = self.mobile_messages()[0][2]
        actions = mobile["data"]["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "URI")
        self.assertEqual(actions[0]["title"], "💳 Оплатить")
        self.assertEqual(
            actions[0]["uri"],
            "https://ha.example/api/umnyeseti/payment/entry123/token123",
        )

        persistent = next(
            c for c in self.hass.services.calls
            if c[0] == "persistent_notification" and c[1] == "create"
        )
        self.assertIn(
            "### [💳 Оплатить](https://ha.example/api/umnyeseti/payment/entry123/token123)",
            persistent[2]["message"],
        )

    async def test_message_matches_human_readable_format(self):
        await self.coordinator._async_handle_tariff_notifications(self.payload(1))
        mobile = self.mobile_messages()[0][2]
        self.assertEqual(mobile["title"], "⚠️ Умные Сети: тариф закончится завтра")
        self.assertEqual(
            mobile["message"],
            "До окончания оплаченного тарифа остался 1 день.\n"
            "Дата окончания: 31.08.2026\n"
            "Лицевой счёт: 12345678\n"
            "Текущий баланс: 150 ₽\n"
            "К оплате для продления: 450 ₽",
        )
        self.assertNotIn("Интернет 500", mobile["message"])
        self.assertNotIn("23:59", mobile["message"])

    async def test_plural_message_for_three_and_five_days(self):
        title3, message3 = self.coordinator._tariff_notification_text(self.payload(3), 3)
        self.assertEqual(title3, "⚠️ Умные Сети: до окончания тарифа 3 дня")
        self.assertTrue(message3.startswith("До окончания оплаченного тарифа осталось 3 дня."))

        title5, message5 = self.coordinator._tariff_notification_text(self.payload(5), 5)
        self.assertEqual(title5, "⚠️ Умные Сети: до окончания тарифа 5 дней")
        self.assertTrue(message5.startswith("До окончания оплаченного тарифа осталось 5 дней."))

    async def test_notification_money_format_is_separate_from_payment_url_format(self):
        payload = self.payload(1)
        payload["balance"] = 100.5
        payload["tariff"]["pay_subscribe"] = 1
        _, message = self.coordinator._tariff_notification_text(payload, 1)
        self.assertIn("Текущий баланс: 100,50 ₽", message)
        self.assertIn("К оплате для продления: 1 ₽", message)
        # Gateway amount formatting stays two-decimal internally even though
        # the human notification intentionally shows whole rubles without .00.
        payment_module = sys.modules["custom_components.umnyeseti.payment"]
        self.assertEqual(payment_module.format_payment_amount(payload["tariff"]["pay_subscribe"]), "1.00")

    async def test_zero_day_notification_is_sent_when_payment_is_still_needed(self):
        await self.coordinator._async_handle_tariff_notifications(self.payload(0))
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], 0)
        self.assertIn("тариф заканчивается сегодня", self.mobile_messages()[-1][2]["title"])
        self.assertIn("Оплаченный тариф заканчивается сегодня.", self.mobile_messages()[-1][2]["message"])

    async def test_zero_day_notification_requires_payment_still_needed(self):
        payload = self.payload(0)
        payload["tariff"]["pay_subscribe"] = 0.0
        await self.coordinator._async_handle_tariff_notifications(payload)
        self.assertEqual(self.mobile_messages(), [])
        self.assertIsNone(self.coordinator._tariff_notification_state["notified_threshold"])

    async def test_zero_day_threshold_survives_quiet_hours(self):
        allowed = False
        self.coordinator._notification_window_open = lambda now=None: allowed
        await self.coordinator._async_handle_tariff_notifications(self.payload(0))
        self.assertEqual(self.mobile_messages(), [])
        self.assertEqual(self.coordinator._tariff_notification_state["pending_threshold"], 0)

        allowed = True
        await self.coordinator._async_handle_tariff_notifications(self.payload(0))
        self.assertEqual(len(self.mobile_messages()), 2)
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], 0)
        self.assertIsNone(self.coordinator._tariff_notification_state["pending_threshold"])

    async def test_overdue_days_one_two_three_are_sent(self):
        await self.coordinator._async_handle_tariff_notifications(self.payload(-1))
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], -1)
        self.assertIn("просрочен на 1 день", self.mobile_messages()[-1][2]["title"])

        await self.coordinator._async_handle_tariff_notifications(self.payload(-2))
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], -2)
        self.assertIn("просрочен на 2 дня", self.mobile_messages()[-1][2]["title"])

        await self.coordinator._async_handle_tariff_notifications(self.payload(-3))
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], -3)
        self.assertIn("просрочен на 3 дня", self.mobile_messages()[-1][2]["title"])

    async def test_full_payment_reminder_sequence_includes_zero_day(self):
        expected = [5, 3, 1, 0, -1, -2, -3]
        for days in expected:
            await self.coordinator._async_handle_tariff_notifications(self.payload(days))
            self.assertEqual(
                self.coordinator._tariff_notification_state["notified_threshold"],
                days,
            )

        # Two configured phones receive one notification for every threshold.
        self.assertEqual(len(self.mobile_messages()), len(expected) * 2)

    async def test_after_third_overdue_day_warning_is_cleared(self):
        await self.coordinator._async_handle_tariff_notifications(self.payload(-3))
        self.hass.services.calls.clear()
        await self.coordinator._async_handle_tariff_notifications(self.payload(-4))
        self.assertTrue(any(c[0] == "persistent_notification" and c[1] == "dismiss" for c in self.hass.services.calls))
        self.assertTrue(any(c[0] == "notify" and c[2].get("message") == "clear_notification" for c in self.hass.services.calls))
        self.assertIsNone(self.coordinator._tariff_notification_state["notified_threshold"])

    async def test_quiet_hours_delay_until_window_opens(self):
        allowed = False
        self.coordinator._notification_window_open = lambda now=None: allowed
        await self.coordinator._async_handle_tariff_notifications(self.payload(5))
        self.assertEqual(self.mobile_messages(), [])
        self.assertEqual(self.coordinator._tariff_notification_state["pending_threshold"], 5)
        self.assertIsNone(self.coordinator._tariff_notification_state["notified_threshold"])

        allowed = True
        await self.coordinator._async_handle_tariff_notifications(self.payload(5))
        self.assertEqual(len(self.mobile_messages()), 2)
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], 5)
        self.assertIsNone(self.coordinator._tariff_notification_state["pending_threshold"])

    async def test_sent_threshold_is_not_requeued_when_quiet_hours_begin(self):
        self.coordinator._notification_window_open = lambda now=None: True
        await self.coordinator._async_handle_tariff_notifications(self.payload(5))
        first_count = len(self.mobile_messages())

        self.coordinator._notification_window_open = lambda now=None: False
        await self.coordinator._async_handle_tariff_notifications(self.payload(5))
        self.assertEqual(len(self.mobile_messages()), first_count)
        self.assertIsNone(self.coordinator._tariff_notification_state["pending_threshold"])

    async def test_quiet_hour_event_survives_day_counter_change(self):
        allowed = False
        self.coordinator._notification_window_open = lambda now=None: allowed
        await self.coordinator._async_handle_tariff_notifications(self.payload(5))
        self.assertEqual(self.coordinator._tariff_notification_state["pending_threshold"], 5)

        # Simulate the provider already reporting 4 days when the window opens.
        allowed = True
        await self.coordinator._async_handle_tariff_notifications(self.payload(4))
        self.assertEqual(len(self.mobile_messages()), 2)
        self.assertIn("осталось 4 дня", self.mobile_messages()[0][2]["message"])
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], 4)
        self.assertIsNone(self.coordinator._tariff_notification_state["pending_threshold"])

    def test_notification_window_is_strict_07_to_22(self):
        from datetime import datetime, timezone
        helper = coordinator_module.UmnyeSetiCoordinator._notification_window_open
        self.assertFalse(helper(datetime(2026, 9, 23, 6, 59, tzinfo=timezone.utc)))
        self.assertTrue(helper(datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc)))
        self.assertTrue(helper(datetime(2026, 9, 23, 21, 59, tzinfo=timezone.utc)))
        self.assertFalse(helper(datetime(2026, 9, 23, 22, 0, tzinfo=timezone.utc)))

    async def test_gateway_handoff_clears_all_notifications_and_suppresses_same_day(self):
        payload = self.payload(3)
        await self.coordinator._async_handle_tariff_notifications(payload)
        self.assertEqual(len(self.mobile_messages()), 2)

        # async_payment_gateway_opened reads the current coordinator payload.
        self.coordinator.data = types.SimpleNamespace(data=payload)
        self.hass.services.calls.clear()
        await self.coordinator.async_payment_gateway_opened()

        self.assertTrue(any(
            c[0] == "persistent_notification" and c[1] == "dismiss"
            for c in self.hass.services.calls
        ))
        clear_pushes = [
            c for c in self.hass.services.calls
            if c[0] == "notify" and c[2].get("message") == "clear_notification"
        ]
        self.assertEqual(len(clear_pushes), 2)
        self.assertEqual(self.coordinator._tariff_notification_state["payment_dismissed_days"], 3)
        self.assertIsNone(self.coordinator._tariff_notification_state["notified_threshold"])
        self.assertFalse(self.coordinator._tariff_notification_state["persistent_sent"])

        # A normal refresh with the same end_days must not recreate anything.
        self.hass.services.calls.clear()
        await self.coordinator._async_handle_tariff_notifications(payload)
        self.assertEqual(self.hass.services.calls, [])


    async def test_no_warning_when_balance_already_covers_tariff(self):
        payload = self.payload(5)
        payload["tariff"]["pay_subscribe"] = 0.0

        await self.coordinator._async_handle_tariff_notifications(payload)

        self.assertEqual(self.mobile_messages(), [])
        self.assertFalse(any(
            c[0] == "persistent_notification" and c[1] == "create"
            for c in self.hass.services.calls
        ))
        self.assertIsNone(self.coordinator._tariff_notification_state["notified_threshold"])

    async def test_existing_warning_is_cleared_when_balance_becomes_sufficient(self):
        await self.coordinator._async_handle_tariff_notifications(self.payload(3))
        self.hass.services.calls.clear()

        payload = self.payload(3)
        payload["tariff"]["pay_subscribe"] = 0.0
        await self.coordinator._async_handle_tariff_notifications(payload)

        self.assertTrue(any(
            c[0] == "persistent_notification" and c[1] == "dismiss"
            for c in self.hass.services.calls
        ))
        self.assertEqual(len([
            c for c in self.hass.services.calls
            if c[0] == "notify" and c[2].get("message") == "clear_notification"
        ]), 2)
        self.assertIsNone(self.coordinator._tariff_notification_state["notified_threshold"])

    async def test_gateway_next_threshold_requires_payment_still_needed(self):
        self.coordinator._tariff_notification_state["payment_dismissed_days"] = 3
        self.coordinator._tariff_notification_state["last_days"] = 3

        # Day counter moves on, but the balance is now sufficient.
        payload = self.payload(1)
        payload["tariff"]["pay_subscribe"] = 0.0
        await self.coordinator._async_handle_tariff_notifications(payload)

        self.assertEqual(self.mobile_messages(), [])
        self.assertIsNone(self.coordinator._tariff_notification_state["payment_dismissed_days"])
        self.assertIsNone(self.coordinator._tariff_notification_state["notified_threshold"])

    async def test_overdue_warning_also_requires_payment_still_needed(self):
        payload = self.payload(-1)
        payload["tariff"]["pay_subscribe"] = 0.0

        await self.coordinator._async_handle_tariff_notifications(payload)

        self.assertEqual(self.mobile_messages(), [])
        self.assertIsNone(self.coordinator._tariff_notification_state["notified_threshold"])

    async def test_gateway_handoff_suppression_ends_when_day_counter_changes(self):
        self.coordinator._tariff_notification_state["payment_dismissed_days"] = 3
        self.coordinator._tariff_notification_state["last_days"] = 3

        # Non-threshold day clears the suppression marker without a notification.
        await self.coordinator._async_handle_tariff_notifications(self.payload(2))
        self.assertIsNone(self.coordinator._tariff_notification_state["payment_dismissed_days"])
        self.assertEqual(self.mobile_messages(), [])

        # The next normal threshold is allowed to notify again if payment has not
        # yet changed the provider's tariff data.
        await self.coordinator._async_handle_tariff_notifications(self.payload(1))
        self.assertEqual(len(self.mobile_messages()), 2)
        self.assertEqual(self.coordinator._tariff_notification_state["notified_threshold"], 1)


if __name__ == "__main__":
    unittest.main()


class RepairIssueRegressionTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = object.__new__(UmnyeSetiCoordinator)
        self.coordinator.hass = object()
        self.coordinator._entry_id = "entry123"

    def test_raise_issue_exists_and_uses_entry_scoped_issue_id(self):
        calls = []
        original = coordinator_module.ir.async_create_issue
        coordinator_module.ir.async_create_issue = lambda *args, **kwargs: calls.append((args, kwargs))
        try:
            self.coordinator._raise_issue("test details")
        finally:
            coordinator_module.ir.async_create_issue = original

        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[1], coordinator_module.DOMAIN)
        self.assertEqual(args[2], "error_entry123")
        self.assertEqual(kwargs["translation_key"], "connection_error")
        self.assertEqual(kwargs["translation_placeholders"], {"error": "test details"})

    def test_clear_issue_exists_and_deletes_entry_scoped_issue_id(self):
        calls = []
        original = coordinator_module.ir.async_delete_issue
        coordinator_module.ir.async_delete_issue = lambda *args, **kwargs: calls.append((args, kwargs))
        try:
            self.coordinator._clear_issue()
        finally:
            coordinator_module.ir.async_delete_issue = original

        self.assertEqual(len(calls), 1)
        args, _kwargs = calls[0]
        self.assertEqual(args[1], coordinator_module.DOMAIN)
        self.assertEqual(args[2], "error_entry123")
