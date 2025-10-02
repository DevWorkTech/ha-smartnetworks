from __future__ import annotations
from typing import Optional
from aiohttp import ClientSession, ClientTimeout
from .const import INIT_URL, AUTH_URL, USER_AGENT_TEMPLATE
import re, json as _json

DEFAULT_TIMEOUT = ClientTimeout(total=15)

class UmnyeSetiApi:
    def _read_version_from_manifest(self) -> str:
        try:
            import json, pathlib
            manifest_path = pathlib.Path(__file__).with_name('manifest.json')
            if manifest_path.exists():
                data = json.loads(manifest_path.read_text(encoding='utf-8'))
                return str(data.get('version') or '0.0.0')
        except Exception:
            pass
        return '0.0.0'


    def _headers_form(self) -> dict:
            return {
                'User-Agent': self.user_agent,
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'Expires': '0',
            }
    
    def _headers_json(self) -> dict:
            return {
                'User-Agent': self.user_agent,
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'Expires': '0',
            }
    
    def _headers_html(self) -> dict:
            return {
                'User-Agent': self.user_agent,
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'Expires': '0',
            }




    def __init__(self, session: ClientSession, *, verify_ssl: bool = True, on_cookies=None, version: str = "0.0.0"):
        self._session = session
        self._verify_ssl = verify_ssl
        self._version = version
        self._last_error: Optional[str] = None
        self._on_cookies = on_cookies

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def user_agent(self) -> str:
        try:
            return USER_AGENT_TEMPLATE.format(version=self._version)
        except Exception:
            return USER_AGENT_TEMPLATE.format(version='0.0.0')

    async def _persist(self):
        if callable(self._on_cookies):
            try:
                await self._on_cookies()
            except Exception:
                pass

    async def auth(self, login: str, password: str):
        self._last_error = None
        async with self._session.get(
            INIT_URL,
            headers=self._headers_html(),
            ssl=self._verify_ssl,
            timeout=DEFAULT_TIMEOUT,
        ) as resp:
            html = await resp.text()

        m = re.search(r'<input[^>]+name=["\']authenticity_token["\'][^>]+value=["\']([^"\']+)["\']', html, re.I)
        if not m:
            self._last_error = "init_token_not_found"
            return {"error": "auth_failed", "message": "init_token_not_found"}

        form = {
            "user[login]": login,
            "user[password]": password,
            "authenticity_token": m.group(1),
            "utf8": "&#x2713;",
            "commit": "Войти",
        }
        async with self._session.post(
            AUTH_URL,
            headers=self._headers_form(), 
            ssl=self._verify_ssl,
            data=form,
            timeout=DEFAULT_TIMEOUT) as resp:
            text = await resp.text()

        await self._persist()

        try:
            return _json.loads(text)
        except Exception:
            pass

        m = re.search(r'<div\s+class=["\']error_container["\']\s*>(.*?)</div>', text, re.I | re.S)
        if m:
            import re as _re
            msg_raw = m.group(1)
            msg = _re.sub(r'<[^>]+>', '', msg_raw).strip()
            self._last_error = "auth_failed"
            return {"error": "auth_failed", "message": msg}

        self._last_error = "auth_failed"
        return {"error": "auth_failed", "message": ""}

    async def fetch_json(self):
        self._last_error = None
        async with self._session.get(
            INIT_URL,
            headers=self._headers_json(),
            ssl=self._verify_ssl,
            timeout=DEFAULT_TIMEOUT,
        ) as resp:
            text = await resp.text()
            if resp.status == 401:
                return {"error": "unauthorized"}

        await self._persist()

        try:
            return _json.loads(text)
        except Exception:
            self._last_error = "invalid_json"
            return {"error": "invalid_json"}
