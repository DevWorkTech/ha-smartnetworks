from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "umnyeseti"

package = types.ModuleType("custom_components")
package.__path__ = []
integration = types.ModuleType("custom_components.umnyeseti")
integration.__path__ = [str(COMPONENT)]
sys.modules["custom_components"] = package
sys.modules["custom_components.umnyeseti"] = integration

spec = importlib.util.spec_from_file_location(
    "custom_components.umnyeseti.payment", COMPONENT / "payment.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

format_payment_amount = module.format_payment_amount
build_payment_bridge_path = module.build_payment_bridge_path
parse_payment_confirmation = module.parse_payment_confirmation
is_safe_yoomoney_url = module.is_safe_yoomoney_url


class PaymentUrlTests(unittest.TestCase):
    def test_amount_is_always_two_decimals(self):
        self.assertEqual(format_payment_amount(1), "1.00")
        self.assertEqual(format_payment_amount(450.0), "450.00")
        self.assertEqual(format_payment_amount("10,5"), "10.50")


    def test_confirm_parser_requires_success_marker_and_yoomoney_link(self):
        body = """
        <div class='alert alert-success'>Проверка введенных данных прошла успешно</div>
        <a class='btn btn-primary' href='https://yoomoney.ru/checkout/payments/v2/contract?orderId=abc'>Продолжить оплату</a>
        """
        self.assertEqual(
            parse_payment_confirmation(body),
            (True, "https://yoomoney.ru/checkout/payments/v2/contract?orderId=abc"),
        )
        self.assertEqual(parse_payment_confirmation("<div>nope</div>"), (False, None))

    def test_confirm_parser_rejects_external_redirect(self):
        body = """
        <div class='alert alert-success'>Проверка введенных данных прошла успешно</div>
        <a class='btn btn-primary' href='https://evil.example/pay'>Продолжить оплату</a>
        """
        self.assertEqual(parse_payment_confirmation(body), (True, None))
        self.assertTrue(is_safe_yoomoney_url("https://yoomoney.ru/checkout/payments/v2/contract?id=1"))
        self.assertFalse(is_safe_yoomoney_url("http://yoomoney.ru/pay"))
        self.assertFalse(is_safe_yoomoney_url("https://yoomoney.ru.evil.example/pay"))

    def test_bridge_path_is_token_protected_and_url_encoded(self):
        self.assertEqual(
            build_payment_bridge_path("entry 1", "abc/123"),
            "/api/umnyeseti/payment/entry%201/abc%2F123",
        )
        self.assertIsNone(build_payment_bridge_path("", "token"))
        self.assertIsNone(build_payment_bridge_path("entry", ""))


if __name__ == "__main__":
    unittest.main()
