from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PaymentActionPositionTests(unittest.TestCase):
    def test_persistent_payment_link_is_before_message(self):
        text = (ROOT / "custom_components" / "umnyeseti" / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn('persistent_message = f"### [💳 {label}]({payment_url})\\n\\n{message}"', text)

    def test_base_sensor_exposes_emoji_payment_action(self):
        text = (ROOT / "custom_components" / "umnyeseti" / "sensor.py").read_text(encoding="utf-8")
        self.assertIn('return {"💳 Оплатить": payment_url} if payment_url else {}', text)
        self.assertNotIn('attrs["Оплатить"] = payment_url', text)


if __name__ == "__main__":
    unittest.main()
