from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "umnyeseti"


def _redact(value, keys):
    if isinstance(value, dict):
        return {
            k: ("REDACTED" if k in keys else _redact(v, keys))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, keys) for v in value]
    return value


def _load_diagnostics_module():
    package = types.ModuleType("custom_components")
    package.__path__ = []
    integration = types.ModuleType("custom_components.umnyeseti")
    integration.__path__ = [str(COMPONENT)]
    sys.modules["custom_components"] = package
    sys.modules["custom_components.umnyeseti"] = integration

    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    diagnostics_ha = types.ModuleType("homeassistant.components.diagnostics")
    config_entries = types.ModuleType("homeassistant.config_entries")
    core = types.ModuleType("homeassistant.core")

    diagnostics_ha.async_redact_data = _redact
    config_entries.ConfigEntry = object
    core.HomeAssistant = object

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.components": components,
            "homeassistant.components.diagnostics": diagnostics_ha,
            "homeassistant.config_entries": config_entries,
            "homeassistant.core": core,
        }
    )

    for name in ("const",):
        spec = importlib.util.spec_from_file_location(
            f"custom_components.umnyeseti.{name}", COMPONENT / f"{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

    coordinator_mod = types.ModuleType("custom_components.umnyeseti.coordinator")

    class UmnyeSetiCoordinator:
        pass

    coordinator_mod.UmnyeSetiCoordinator = UmnyeSetiCoordinator
    sys.modules["custom_components.umnyeseti.coordinator"] = coordinator_mod

    spec = importlib.util.spec_from_file_location(
        "custom_components.umnyeseti.diagnostics", COMPONENT / "diagnostics.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeState:
    def __init__(self):
        self.data = {"account": "00123456", "balance": 100}
        self.error = None
        self.last_attempt = "2026-09-24T00:00:00+00:00"
        self.error_details = None


class FakeApi:
    last_error = None
    last_error_details = {}
    raw_debug = {
        "auth.submit": {
            "request": {
                "body": {
                    "user[login]": "00123456",
                    "user[password]": "[redacted]",
                    "authenticity_token": "[redacted]",
                }
            },
            "response": {"status": 200, "body": '{"success":true}'},
        },
        "data.fetch": {
            "response": {"status": 200, "body": '{"data":{"account":"00123456"}}'}
        },
    }


class FakeCoordinator:
    def __init__(self):
        self.data = FakeState()
        self.api = FakeApi()
        self.update_interval = types.SimpleNamespace(total_seconds=lambda: 1800)
        self._verify_ssl = False


class FakeEntry:
    entry_id = "entry123"
    title = "Умные Сети"
    data = {
        "login": "00123456",
        "password": "secret-password",
        "payment_link_token": "secret-bridge-token",
    }
    options = {"update_interval": 30, "verify_ssl": False}


class FakeHass:
    data = {
        "umnyeseti": {
            "entry123": FakeCoordinator(),
            "manifest_version": "1.0.21",
        }
    }


class DiagnosticsTests(unittest.TestCase):
    def test_standard_diagnostics_contains_always_on_raw_and_redacts_entry_secrets(self):
        mod = _load_diagnostics_module()
        result = asyncio.run(
            mod.async_get_config_entry_diagnostics(FakeHass(), FakeEntry())
        )

        self.assertEqual(result["integration"]["version"], "1.0.21")
        self.assertEqual(result["entry_data"]["login"], "00123456")
        self.assertEqual(result["entry_data"]["password"], "REDACTED")
        self.assertEqual(result["entry_data"]["payment_link_token"], "REDACTED")
        self.assertIn("auth.submit", result["raw_http"])
        self.assertIn("data.fetch", result["raw_http"])
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-password", text)
        self.assertNotIn("secret-bridge-token", text)
        self.assertIn("[redacted]", text)

    def test_raw_sensor_is_removed_from_sensor_platform(self):
        text = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
        self.assertNotIn("class RawDebugSensor", text)
        self.assertNotIn("RawDebugSensor(coordinator, entry)", text)
        self.assertIn("raw_unique_id", text)
        self.assertIn("registry.async_remove", text)


if __name__ == "__main__":
    unittest.main()
