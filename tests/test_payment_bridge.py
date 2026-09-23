from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "umnyeseti"


def _load_module():
    package = types.ModuleType("custom_components")
    package.__path__ = []
    integration = types.ModuleType("custom_components.umnyeseti")
    integration.__path__ = [str(COMPONENT)]
    sys.modules["custom_components"] = package
    sys.modules["custom_components.umnyeseti"] = integration

    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    http = types.ModuleType("homeassistant.components.http")
    view = types.ModuleType("homeassistant.components.http.view")

    http.KEY_HASS = "hass"

    class HomeAssistantView:
        pass

    view.HomeAssistantView = HomeAssistantView
    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.components": components,
            "homeassistant.components.http": http,
            "homeassistant.components.http.view": view,
        }
    )

    for name in ("const", "payment"):
        spec = importlib.util.spec_from_file_location(
            f"custom_components.umnyeseti.{name}", COMPONENT / f"{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

    spec = importlib.util.spec_from_file_location(
        "custom_components.umnyeseti.payment_bridge", COMPONENT / "payment_bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bridge_module = _load_module()
BridgeView = bridge_module.UmnyeSetiPaymentBridgeView
web = bridge_module.web


class Entry:
    domain = "umnyeseti"
    data = {"payment_link_token": "token123"}


class Entries:
    def async_get_entry(self, entry_id):
        return Entry() if entry_id == "entry123" else None


class State:
    data = {
        "account": "00123456",
        "tariff": {"pay_subscribe": 1, "end_days": 3},
    }


class FakeApi:
    user_agent = "UmnyeSeti/Test"

    def __init__(self):
        self.raw = []

    def record_raw_http(self, *args, **kwargs):
        self.raw.append((args, kwargs))


class FakeResponse:
    def __init__(self, *, status=200, text=""):
        self.status = status
        self.url = "https://sberpay.umnyeseti.ru/ya_kassa/confirm"
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.history = []
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self, errors="replace"):
        return self._text


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class Coordinator:
    data = State()
    _verify_ssl = False

    def __init__(self):
        self.gateway_opened_calls = 0
        body = """
        <div class='alert alert-success'>Проверка введенных данных прошла успешно</div>
        <a class='btn btn-primary' href='https://yoomoney.ru/checkout/payments/v2/contract?orderId=test-order' target='_blank'>Продолжить оплату</a>
        """
        self.api = FakeApi()
        self.session = FakeSession(FakeResponse(text=body))

    async def async_payment_gateway_opened(self):
        self.gateway_opened_calls += 1


class Hass:
    config_entries = Entries()

    def __init__(self):
        self.coordinator = Coordinator()
        self.data = {"umnyeseti": {"entry123": self.coordinator}}


class Request:
    def __init__(self, form=None, hass=None):
        self._form = form or {}
        self.hass = hass or Hass()
        self.app = {"hass": self.hass}

    async def post(self):
        return self._form


class PaymentBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_shows_ha_style_form_with_disabled_account_and_editable_amount(self):
        view = BridgeView()
        response = await view.get(Request(), "entry123", "token123")
        text = response.text
        self.assertEqual(response.status, 200)
        self.assertIn('value="00123456" disabled', text)
        self.assertIn('<select id="payment-method" disabled aria-disabled="true">', text)
        self.assertIn('<option selected>Оплата 🏦 СБП / 💳 Картой</option>', text)
        self.assertEqual(text.count('Оплата 🏦 СБП / 💳 Картой'), 1)
        self.assertIn('name="amount"', text)
        self.assertIn('value="1.00"', text)
        self.assertIn('>💳 Оплатить</button>', text)
        self.assertIn('После нажатия откроется защищённая страница оплаты', text)
        self.assertIn('Умные Сети', text)
        self.assertIn('Home Assistant', text)
        self.assertIn("Оплата уже открыта", text)
        self.assertIn("back_forward", text)
        self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")

    async def test_post_uses_server_account_posts_confirm_and_redirects_to_yoomoney(self):
        view = BridgeView()
        hass = Hass()
        response = await view.post(
            Request({"account": "99999999", "amount": "25,5"}, hass=hass),
            "entry123",
            "token123",
        )
        text = response.text
        self.assertEqual(response.status, 200)
        self.assertIn(
            "https://yoomoney.ru/checkout/payments/v2/contract?orderId=test-order",
            text,
        )
        self.assertIn("location.replace", text)
        self.assertNotIn("account=99999999", text)

        self.assertEqual(len(hass.coordinator.session.calls), 1)
        url, kwargs = hass.coordinator.session.calls[0]
        self.assertEqual(url, "https://sberpay.umnyeseti.ru/ya_kassa/confirm")
        self.assertEqual(
            kwargs["data"],
            {
                "account": "00123456",
                "amount": "25.50",
                "mode": "",
                "user_account": "00123456",
                "locale": "",
            },
        )
        self.assertEqual(len(hass.coordinator.api.raw), 1)
        self.assertEqual(hass.coordinator.api.raw[0][0][0], "payment.confirm")
        self.assertEqual(hass.coordinator.gateway_opened_calls, 1)

    async def test_confirm_without_success_marker_returns_simple_error(self):
        hass = Hass()
        hass.coordinator.session = FakeSession(FakeResponse(text="<html>Ошибка проверки</html>"))
        response = await BridgeView().post(Request({"amount": "25"}, hass=hass), "entry123", "token123")
        self.assertEqual(response.status, 400)
        self.assertIn("Платёжный сервис не подтвердил введённые данные", response.text)

    async def test_confirm_success_without_link_returns_simple_error(self):
        hass = Hass()
        hass.coordinator.session = FakeSession(FakeResponse(text="<div class='alert alert-success'>Проверка введенных данных прошла успешно</div>"))
        response = await BridgeView().post(Request({"amount": "25"}, hass=hass), "entry123", "token123")
        self.assertEqual(response.status, 400)
        self.assertIn("ссылка оплаты не получена", response.text)

    async def test_invalid_amount_returns_simple_error(self):
        view = BridgeView()
        response = await view.post(Request({"amount": "0"}), "entry123", "token123")
        self.assertEqual(response.status, 400)
        self.assertIn("Укажите корректную сумму оплаты больше 0", response.text)

    async def test_invalid_token_is_hidden_as_not_found(self):
        view = BridgeView()
        with self.assertRaises(web.HTTPNotFound):
            await view.get(Request(), "entry123", "wrong")


if __name__ == "__main__":
    unittest.main()
