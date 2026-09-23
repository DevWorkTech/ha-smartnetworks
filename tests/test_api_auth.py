from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "umnyeseti"

# Load the API module without importing the Home Assistant-dependent package
# __init__.py. This keeps the protocol tests runnable with plain Python+aiohttp.
custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(ROOT / "custom_components")]
sys.modules.setdefault("custom_components", custom_components)

umnyeseti = types.ModuleType("custom_components.umnyeseti")
umnyeseti.__path__ = [str(PKG_DIR)]
sys.modules.setdefault("custom_components.umnyeseti", umnyeseti)

api_module = importlib.import_module("custom_components.umnyeseti.api")
UmnyeSetiApi = api_module.UmnyeSetiApi


class FakeCookie:
    def __init__(self, key: str):
        self.key = key


class FakeCookieJar:
    def __init__(self):
        self.names = ["_session"]
        self.cleared = False

    def __iter__(self):
        return iter(FakeCookie(name) for name in self.names)

    def clear(self):
        self.cleared = True
        self.names = []


class FakeResponse:
    def __init__(self, status: int, url: str, text: str = "", headers=None, history=None):
        self.status = status
        self.url = url
        self._text = text
        self.headers = headers or {}
        self.history = history or []

    async def text(self, errors="replace"):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, gets, posts):
        self.gets = list(gets)
        self.posts = list(posts)
        self.cookie_jar = FakeCookieJar()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if not self.gets:
            raise AssertionError("Unexpected GET")
        return self.gets.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if not self.posts:
            raise AssertionError("Unexpected POST")
        return self.posts.pop(0)


LOGIN_HTML = """
<!doctype html>
<html>
<head><meta name="csrf-token" content="meta-token"></head>
<body>
<form method="post" action="/login">
  <input type="hidden" name="utf8" value="✓">
  <input type="hidden" name="authenticity_token" value="form-token">
  <input name="user[login]" type="text">
  <input name="user[password]" type="password">
  <input type="submit" name="commit" value="Войти">
</form>
</body>
</html>
"""

DATA_JSON = json.dumps({"data": {"personal_accounts": []}})


class ApiAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_redirect_login_is_verified(self):
        session = FakeSession(
            gets=[
                FakeResponse(200, "https://stat.umnyeseti.ru/login", LOGIN_HTML, {"Content-Type": "text/html"}),
                FakeResponse(200, "https://stat.umnyeseti.ru", DATA_JSON, {"Content-Type": "application/json"}),
            ],
            posts=[
                FakeResponse(302, "https://stat.umnyeseti.ru/login", "", {"Location": "/", "Content-Type": "text/html"}),
            ],
        )
        api = UmnyeSetiApi(session)
        result = await api.auth("12345678", "secret-password")

        self.assertTrue(result.get("success"))
        self.assertIn("verified_response", result)
        post = [call for call in session.calls if call[0] == "POST"][0]
        self.assertEqual(post[1], "https://stat.umnyeseti.ru/login")
        self.assertFalse(post[2]["allow_redirects"])
        self.assertEqual(post[2]["data"]["utf8"], "✓")
        self.assertEqual(post[2]["data"]["authenticity_token"], "form-token")
        self.assertEqual(post[2]["headers"]["X-CSRF-Token"], "form-token")

    async def test_ajax_json_login_is_verified(self):
        session = FakeSession(
            gets=[
                FakeResponse(200, "https://stat.umnyeseti.ru/login", LOGIN_HTML, {"Content-Type": "text/html"}),
                FakeResponse(200, "https://stat.umnyeseti.ru", DATA_JSON, {"Content-Type": "application/json"}),
            ],
            posts=[
                FakeResponse(200, "https://stat.umnyeseti.ru/login", '{"success":true}', {"Content-Type": "application/json"}),
            ],
        )
        api = UmnyeSetiApi(session)
        result = await api.auth("12345678", "secret-password")
        self.assertTrue(result.get("success"))

    async def test_redirect_back_to_login_is_auth_failure(self):
        session = FakeSession(
            gets=[FakeResponse(200, "https://stat.umnyeseti.ru/login", LOGIN_HTML, {"Content-Type": "text/html"})],
            posts=[FakeResponse(302, "https://stat.umnyeseti.ru/login", "", {"Location": "/login"})],
        )
        api = UmnyeSetiApi(session)
        result = await api.auth("12345678", "bad-password")
        self.assertEqual(result.get("error"), "auth_failed")
        self.assertEqual(api.last_error, "auth_failed")
        self.assertEqual(len([c for c in session.calls if c[0] == "GET"]), 1)

    async def test_explicit_json_failure_is_auth_failure(self):
        session = FakeSession(
            gets=[FakeResponse(200, "https://stat.umnyeseti.ru/login", LOGIN_HTML, {"Content-Type": "text/html"})],
            posts=[FakeResponse(200, "https://stat.umnyeseti.ru/login", '{"success":false,"message":"Denied"}', {"Content-Type": "application/json"})],
        )
        api = UmnyeSetiApi(session)
        result = await api.auth("12345678", "bad-password")
        self.assertEqual(result.get("error"), "auth_failed")
        self.assertEqual(result.get("message"), "Denied")

    async def test_candidate_success_without_authenticated_session_fails(self):
        session = FakeSession(
            gets=[
                FakeResponse(200, "https://stat.umnyeseti.ru/login", LOGIN_HTML, {"Content-Type": "text/html"}),
                FakeResponse(200, "https://stat.umnyeseti.ru/login", LOGIN_HTML, {"Content-Type": "text/html"}),
            ],
            posts=[FakeResponse(200, "https://stat.umnyeseti.ru/login", '{"success":true}', {"Content-Type": "application/json"})],
        )
        api = UmnyeSetiApi(session)
        result = await api.auth("12345678", "secret-password")
        self.assertEqual(result.get("error"), "auth_failed")
        self.assertEqual(api.last_error, "auth_not_established")

    async def test_authenticated_json_must_contain_data(self):
        session = FakeSession(
            gets=[
                FakeResponse(200, "https://stat.umnyeseti.ru/login", LOGIN_HTML, {"Content-Type": "text/html"}),
                FakeResponse(200, "https://stat.umnyeseti.ru", '{"success":true}', {"Content-Type": "application/json"}),
            ],
            posts=[FakeResponse(200, "https://stat.umnyeseti.ru/login", '{"success":true}', {"Content-Type": "application/json"})],
        )
        api = UmnyeSetiApi(session)
        result = await api.auth("12345678", "secret-password")
        self.assertEqual(result.get("error"), "cannot_connect")
        self.assertEqual(api.last_error, "auth_verify_unexpected_payload")

    async def test_fetch_login_page_is_unauthorized(self):
        session = FakeSession(
            gets=[FakeResponse(200, "https://stat.umnyeseti.ru/login", LOGIN_HTML, {"Content-Type": "text/html"})],
            posts=[],
        )
        api = UmnyeSetiApi(session)
        result = await api.fetch_json()
        self.assertEqual(result.get("error"), "unauthorized")

    async def test_invalid_json_is_not_reported_as_unauthorized(self):
        session = FakeSession(
            gets=[FakeResponse(200, "https://stat.umnyeseti.ru", "not-json", {"Content-Type": "text/plain"})],
            posts=[],
        )
        api = UmnyeSetiApi(session)
        result = await api.fetch_json()
        self.assertEqual(result.get("error"), "invalid_json")
        self.assertEqual(api.last_error, "invalid_json")

    async def test_external_form_action_is_rejected(self):
        html = LOGIN_HTML.replace('action="/login"', 'action="https://evil.example/login"')
        session = FakeSession(
            gets=[FakeResponse(200, "https://stat.umnyeseti.ru/login", html, {"Content-Type": "text/html"})],
            posts=[],
        )
        api = UmnyeSetiApi(session)
        result = await api.auth("12345678", "secret-password")
        self.assertEqual(result.get("error"), "cannot_connect")
        self.assertEqual(api.last_error, "unsafe_auth_action")

    async def test_raw_debug_is_always_captured_and_redacts_auth_secrets(self):
        session = FakeSession(
            gets=[
                FakeResponse(200, "https://stat.umnyeseti.ru/login", LOGIN_HTML, {"Content-Type": "text/html", "Set-Cookie": "secret-cookie"}),
                FakeResponse(200, "https://stat.umnyeseti.ru", DATA_JSON, {"Content-Type": "application/json"}),
            ],
            posts=[
                FakeResponse(200, "https://stat.umnyeseti.ru/login", '{"success":true,"token":"server-secret"}', {"Content-Type": "application/json"}),
            ],
        )
        api = UmnyeSetiApi(session)
        result = await api.auth("12345678", "secret-password")
        self.assertTrue(result.get("success"))

        raw = api.raw_debug
        self.assertIn("auth.init", raw)
        self.assertIn("auth.submit", raw)
        self.assertIn("auth.verify", raw)
        self.assertEqual(raw["auth.submit"]["request"]["body"]["user[login]"], "12345678")
        self.assertEqual(raw["auth.submit"]["request"]["body"]["user[password]"], "[redacted]")
        self.assertEqual(raw["auth.submit"]["request"]["body"]["authenticity_token"], "[redacted]")
        self.assertEqual(raw["auth.init"]["response"]["headers"]["Set-Cookie"], "[redacted]")
        raw_text = json.dumps(raw, ensure_ascii=False)
        self.assertNotIn("secret-password", raw_text)
        self.assertNotIn("form-token", raw_text)
        self.assertNotIn("secret-cookie", raw_text)
        self.assertNotIn("server-secret", raw_text)

    async def test_error_snippet_redacts_current_credentials(self):
        session = FakeSession(gets=[], posts=[])
        api = UmnyeSetiApi(session)
        api._set_sensitive("12345678", "secret-password", "csrf-secret")
        sanitized = api._sanitize_snippet(
            'login=12345678 password=secret-password authenticity_token=csrf-secret'
        )
        self.assertNotIn("12345678", sanitized)
        self.assertNotIn("secret-password", sanitized)
        self.assertNotIn("csrf-secret", sanitized)
        self.assertIn("[redacted]", sanitized)


if __name__ == "__main__":
    unittest.main()
