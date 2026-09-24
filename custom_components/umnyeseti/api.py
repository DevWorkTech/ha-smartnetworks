from __future__ import annotations

import asyncio
import copy
import html as html_lib
import json as _json
import logging
import re
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from .const import AUTH_URL, INIT_URL, USER_AGENT_TEMPLATE

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = ClientTimeout(total=20, connect=10, sock_read=15)
MAX_RESPONSE_SNIPPET = 500
AUTH_HOST = "stat.umnyeseti.ru"


class _LoginFormParser(HTMLParser):
    """Find the login form and Rails authenticity/CSRF token."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csrf_token: Optional[str] = None
        self.login_form: Optional[dict[str, Any]] = None
        self._current_form: Optional[dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {str(k).lower(): v for k, v in attrs}
        tag = tag.lower()

        if tag == "meta" and attrs_dict.get("name") in ("csrf-token", "csrf_token"):
            value = attrs_dict.get("content")
            if value:
                self.csrf_token = value
            return

        if tag == "form":
            self._current_form = {
                "action": attrs_dict.get("action") or "",
                "method": (attrs_dict.get("method") or "post").lower(),
                "fields": {},
                "input_names": set(),
            }
            return

        if tag != "input" or self._current_form is None:
            return

        name = attrs_dict.get("name")
        if not name:
            return

        self._current_form["input_names"].add(name)
        value = attrs_dict.get("value")
        if value is not None:
            self._current_form["fields"][name] = value

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "form" or self._current_form is None:
            return

        names = self._current_form.get("input_names", set())
        if "user[login]" in names and "user[password]" in names:
            self.login_form = self._current_form

        self._current_form = None


class UmnyeSetiApi:
    def __init__(
        self,
        session: ClientSession,
        *,
        verify_ssl: bool = False,
        on_cookies=None,
        version: str = "0.0.0",
    ):
        self._session = session
        self._verify_ssl = verify_ssl
        self._version = version if version and version != "0.0.0" else self._read_version_from_manifest()
        self._last_error: Optional[str] = None
        self._last_error_details: dict[str, Any] = {}
        self._on_cookies = on_cookies
        self._sensitive_values: set[str] = set()
        self._raw_debug: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _read_version_from_manifest() -> str:
        try:
            manifest = Path(__file__).with_name("manifest.json")
            data = _json.loads(manifest.read_text(encoding="utf-8"))
            return str(data.get("version") or "0.0.0")
        except Exception:
            return "0.0.0"

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def last_error_details(self) -> dict[str, Any]:
        return dict(self._last_error_details)

    @property
    def raw_debug(self) -> dict[str, dict[str, Any]]:
        """Last sanitized RAW HTTP request/response per API stage.

        RAW capture is always enabled. Authentication secrets, cookies and CSRF
        tokens are masked, while provider payload data remains available for
        troubleshooting from Home Assistant.
        """
        return copy.deepcopy(self._raw_debug)

    @property
    def user_agent(self) -> str:
        try:
            return USER_AGENT_TEMPLATE.format(version=self._version)
        except Exception:
            return USER_AGENT_TEMPLATE.format(version="0.0.0")

    def _headers_form(self, referer: str, csrf_token: Optional[str] = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Expires": "0",
            "Origin": f"https://{AUTH_HOST}",
            "Referer": referer,
        }
        if csrf_token:
            # Rails accepts the token in the form body and/or in this header.
            headers["X-CSRF-Token"] = csrf_token
        return headers

    def _headers_json(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Expires": "0",
            "Referer": f"https://{AUTH_HOST}/",
        }

    def _headers_html(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Expires": "0",
        }

    def _clear_error(self) -> None:
        self._last_error = None
        self._last_error_details = {}

    def _set_sensitive(self, *values: Optional[str]) -> None:
        self._sensitive_values = {str(v) for v in values if v not in (None, "")}

    def _add_sensitive(self, *values: Optional[str]) -> None:
        self._sensitive_values.update(str(v) for v in values if v not in (None, ""))

    def _clear_sensitive(self) -> None:
        self._sensitive_values.clear()

    def _clear_cookies(self) -> None:
        """The API session is dedicated to this integration, so a full clear is safe."""
        try:
            self._session.cookie_jar.clear()
        except Exception as exc:
            _LOGGER.debug(
                "Umnye Seti auth: unable to clear old cookies; exception_type=%s; exception=%s",
                type(exc).__name__,
                exc,
            )

    def _cookie_names(self) -> list[str]:
        """Return cookie names only. Cookie values must never be logged."""
        try:
            return sorted({cookie.key for cookie in self._session.cookie_jar})
        except Exception:
            return []

    @staticmethod
    def _redirect_chain(resp: ClientResponse) -> list[str]:
        chain: list[str] = []
        try:
            for item in resp.history:
                location = item.headers.get("Location")
                suffix = f" -> {location}" if location else ""
                chain.append(f"{item.status} {item.url}{suffix}")
        except Exception:
            return []
        return chain

    def _sanitize_snippet(self, text: str) -> str:
        if not text:
            return ""

        snippet = text[:4000]

        # First remove exact values known for the current authorization attempt.
        for value in sorted(self._sensitive_values, key=len, reverse=True):
            if not value:
                continue
            snippet = snippet.replace(value, "[redacted]")
            encoded = quote_plus(value)
            if encoded != value:
                snippet = snippet.replace(encoded, "[redacted]")

        patterns = (
            # HTML fields/meta values.
            (r'(?is)(name=["\'](?:authenticity_token|csrf-token|csrf_token|user\[password\]|user\[login\]|password|username|login)["\'][^>]*?value=["\'])[^"\']+', r"\1[redacted]"),
            (r'(?is)(value=["\'])[^"\']+(["\'][^>]*?name=["\'](?:authenticity_token|csrf-token|csrf_token|user\[password\]|user\[login\]|password|username|login)["\'])', r"\1[redacted]\2"),
            (r'(?is)(name=["\'](?:csrf-token|csrf_token)["\'][^>]*?content=["\'])[^"\']+', r"\1[redacted]"),
            # Common JSON/query-style secrets.
            (r'(?i)("?(?:user\[password\]|user\[login\]|password|username|login|token|secret|authorization|cookie|authenticity_token)"?\s*[:=]\s*["\']?)[^"\'&,\s<]+', r"\1[redacted]"),
        )
        for pattern, repl in patterns:
            snippet = re.sub(pattern, repl, snippet)

        snippet = re.sub(r"(?is)<script\b.*?</script>", " ", snippet)
        snippet = re.sub(r"(?is)<style\b.*?</style>", " ", snippet)
        snippet = re.sub(r"(?s)<[^>]+>", " ", snippet)
        snippet = html_lib.unescape(snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip()
        return snippet[:MAX_RESPONSE_SNIPPET]

    @staticmethod
    def _sanitize_raw_headers(headers: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            items = headers.items()
        except Exception:
            return out
        for key, value in items:
            name = str(key)
            low = name.lower()
            if low in {"authorization", "cookie", "set-cookie", "x-csrf-token", "x-xsrf-token"}:
                out[name] = "[redacted]"
            else:
                out[name] = str(value)
        return out

    @classmethod
    def _sanitize_raw_value(cls, value: Any, key: str = "") -> Any:
        low = str(key).lower()
        secret_markers = ("password", "passwd", "token", "secret", "cookie", "authorization", "authenticity", "csrf", "xsrf")
        if any(marker in low for marker in secret_markers):
            return "[redacted]"
        if isinstance(value, dict):
            return {str(k): cls._sanitize_raw_value(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_raw_value(v) for v in value]
        return value

    @staticmethod
    def _sanitize_raw_text(text: str) -> str:
        """Mask authentication secrets without flattening the RAW body."""
        if not text:
            return ""
        result = str(text)
        patterns = (
            (r'(?is)(name=["\'](?:authenticity_token|csrf-token|csrf_token|user\[password\]|password)["\'][^>]*?(?:value|content)=["\'])[^"\']+', r"\1[redacted]"),
            (r'(?is)((?:value|content)=["\'])[^"\']+(["\'][^>]*?name=["\'](?:authenticity_token|csrf-token|csrf_token|user\[password\]|password)["\'])', r"\1[redacted]\2"),
            (r'(?i)("?(?:user\[password\]|password|token|secret|authorization|cookie|authenticity_token|csrf_token|csrf-token)"?\s*[:=]\s*["\']?)[^"\'&,\s<]+', r"\1[redacted]"),
        )
        for pattern, repl in patterns:
            result = re.sub(pattern, repl, result)
        return result

    def record_raw_http(
        self,
        stage: str,
        *,
        method: str,
        url: str,
        request_headers: Optional[dict[str, Any]] = None,
        request_body: Any = None,
        resp: Optional[ClientResponse] = None,
        response_text: str = "",
        exc: Optional[BaseException] = None,
    ) -> None:
        """Expose sanitized RAW capture for auxiliary integration HTTP flows."""
        self._record_raw(
            stage,
            method=method,
            url=url,
            request_headers=request_headers,
            request_body=request_body,
            resp=resp,
            response_text=response_text,
            exc=exc,
        )

    def _record_raw(
        self,
        stage: str,
        *,
        method: str,
        url: str,
        request_headers: Optional[dict[str, Any]] = None,
        request_body: Any = None,
        resp: Optional[ClientResponse] = None,
        response_text: str = "",
        exc: Optional[BaseException] = None,
    ) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request": {
                "method": str(method).upper(),
                "url": str(url),
                "headers": self._sanitize_raw_headers(request_headers or {}),
            },
        }
        if request_body is not None:
            record["request"]["body"] = self._sanitize_raw_value(request_body)
        if resp is not None:
            record["response"] = {
                "status": int(resp.status),
                "url": str(resp.url),
                "headers": self._sanitize_raw_headers(resp.headers),
                "redirects": self._redirect_chain(resp),
                "body": self._sanitize_raw_text(response_text),
            }
        elif exc is not None:
            record["response"] = {
                "exception_type": type(exc).__name__,
                "exception": str(exc) or type(exc).__name__,
            }
        self._raw_debug[str(stage)] = record

    @staticmethod
    def _response_meta(resp: ClientResponse, text: str) -> dict[str, Any]:
        content_type = resp.headers.get("Content-Type", "")
        return {
            "http_status": resp.status,
            "url": str(resp.url),
            "content_type": content_type,
            "response_length": len(text.encode("utf-8", errors="replace")),
            "location": resp.headers.get("Location"),
            "redirects": UmnyeSetiApi._redirect_chain(resp),
        }

    def _trace_response(self, stage: str, resp: ClientResponse, text: str) -> None:
        meta = self._response_meta(resp, text)
        _LOGGER.debug(
            "Umnye Seti stage=%s: status=%s; url=%s; location=%s; redirects=%s; "
            "content_type=%s; response_length=%s; cookie_names=%s",
            stage,
            meta.get("http_status"),
            meta.get("url"),
            meta.get("location"),
            meta.get("redirects"),
            meta.get("content_type"),
            meta.get("response_length"),
            self._cookie_names(),
        )

    def _set_error(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        resp: Optional[ClientResponse] = None,
        text: str = "",
        exc: Optional[BaseException] = None,
        level: int = logging.ERROR,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        details: dict[str, Any] = {
            "code": code,
            "stage": stage,
            "message": message or code,
        }

        if resp is not None:
            details.update(self._response_meta(resp, text))
        if text:
            snippet = self._sanitize_snippet(text)
            if snippet:
                details["response_snippet"] = snippet
        if exc is not None:
            details["exception_type"] = type(exc).__name__
            details["exception"] = self._sanitize_snippet(str(exc) or type(exc).__name__)
        if extra:
            details.update(extra)

        details["cookie_names"] = self._cookie_names()
        self._last_error = code
        self._last_error_details = details

        _LOGGER.log(level, "Umnye Seti request failed: %s", self.format_error_details(details))
        return details

    @staticmethod
    def format_error_details(details: Optional[dict[str, Any]] = None) -> str:
        data = details or {}
        keys = (
            "stage",
            "code",
            "http_status",
            "url",
            "location",
            "content_type",
            "response_length",
            "redirects",
            "exception_type",
            "exception",
            "message",
            "response_snippet",
            "cookie_names",
        )
        parts: list[str] = []
        for key in keys:
            value = data.get(key)
            if value in (None, "", [], {}):
                continue
            parts.append(f"{key}={value}")
        return "; ".join(parts)

    @staticmethod
    def _parse_login_page(text: str) -> dict[str, Any]:
        parser = _LoginFormParser()
        try:
            parser.feed(text)
        except Exception:
            pass

        form = parser.login_form or {
            "action": "",
            "method": "post",
            "fields": {},
            "input_names": set(),
        }
        fields = dict(form.get("fields") or {})
        token = fields.get("authenticity_token") or parser.csrf_token

        if not token:
            # Fallback for malformed HTML or unusual attribute ordering.
            match = re.search(
                r'(?is)<input\b(?=[^>]*\bname=["\']authenticity_token["\'])[^>]*\bvalue=["\']([^"\']+)["\']',
                text,
            )
            if match:
                token = html_lib.unescape(match.group(1))

        return {
            "action": form.get("action") or "",
            "method": form.get("method") or "post",
            "fields": fields,
            "input_names": set(form.get("input_names") or set()),
            "token": token,
            "form_found": parser.login_form is not None,
        }

    @staticmethod
    def _extract_html_error(text: str) -> Optional[str]:
        patterns = (
            r'<div\s+class=["\'][^"\']*error_container[^"\']*["\'][^>]*>(.*?)</div>',
            r'<[^>]+class=["\'][^"\']*(?:alert-danger|alert-error|form-error|errors?|error)[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I | re.S)
            if not match:
                continue
            msg = re.sub(r"<[^>]+>", " ", match.group(1))
            msg = html_lib.unescape(re.sub(r"\s+", " ", msg)).strip()
            if msg:
                return msg
        return None

    @staticmethod
    def _looks_like_login_page(resp: ClientResponse, text: str) -> bool:
        try:
            path = urlparse(str(resp.url)).path.rstrip("/")
            if path == "/login":
                return True
        except Exception:
            pass

        low = text.lower()
        has_login = "user[login]" in low or 'name="login"' in low or "name='login'" in low
        has_password = "user[password]" in low or 'type="password"' in low or "type='password'" in low
        return has_login and has_password

    @staticmethod
    def _is_safe_auth_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.hostname == AUTH_HOST

    @staticmethod
    def _is_login_url(url: str) -> bool:
        try:
            return urlparse(url).path.rstrip("/") == "/login"
        except Exception:
            return False

    @staticmethod
    def _auth_failure_from_json(payload: dict[str, Any]) -> Optional[str]:
        def _message() -> str:
            for key in ("message", "reason", "error_description", "error"):
                value = payload.get(key)
                if value not in (None, "", False):
                    return str(value)
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                return "; ".join(str(v) for v in errors)
            if isinstance(errors, dict) and errors:
                return "; ".join(f"{k}: {v}" for k, v in errors.items())
            return "Authorization rejected by server"

        if payload.get("error") not in (None, "", False):
            return _message()
        for key in ("success", "ok", "authenticated", "authorized"):
            if payload.get(key) is False:
                return _message()
        status = str(payload.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure", "unauthorized", "forbidden", "invalid"}:
            return _message()
        return None

    async def _persist(self) -> None:
        if not callable(self._on_cookies):
            return
        try:
            await self._on_cookies()
        except Exception as exc:
            _LOGGER.warning(
                "Umnye Seti cookies could not be persisted: exception_type=%s; exception=%s",
                type(exc).__name__,
                exc,
            )

    async def _verify_auth(self, auth_response: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Verify that login actually produced an authenticated data session."""
        _LOGGER.debug("Umnye Seti auth stage=auth.verify: checking authenticated JSON endpoint")
        payload = await self._fetch_json(stage="auth.verify", clear_error=False, unauthorized_level=logging.ERROR)

        if isinstance(payload, dict) and not payload.get("error"):
            if "data" not in payload:
                details = {
                    "code": "auth_verify_unexpected_payload",
                    "stage": "auth.verify",
                    "message": "Authenticated endpoint returned JSON without the expected data field",
                    "payload_keys": sorted(str(k) for k in payload.keys()),
                    "cookie_names": self._cookie_names(),
                }
                self._last_error = details["code"]
                self._last_error_details = details
                _LOGGER.error("Umnye Seti request failed: %s", self.format_error_details(details))
                return {
                    "error": "cannot_connect",
                    "message": "Авторизация выполнена, но сервер вернул неожиданный формат данных",
                }

            self._clear_error()
            await self._persist()
            _LOGGER.debug("Umnye Seti auth stage=auth.complete: authenticated session verified")
            result: dict[str, Any] = {"success": True, "verified_response": payload}
            if isinstance(auth_response, dict):
                result["auth_response"] = auth_response
            return result

        error_code = payload.get("error") if isinstance(payload, dict) else "invalid_response"
        message = payload.get("message") if isinstance(payload, dict) else "Authorization verification failed"

        # _fetch_json already populated detailed diagnostics. Reclassify only an
        # authorization failure; transport/data errors must not be reported as a
        # bad password.
        if error_code == "unauthorized":
            details = dict(self._last_error_details)
            details.update(
                {
                    "code": "auth_not_established",
                    "stage": "auth.verify",
                    "message": "Credentials were submitted, but the authenticated session was not established",
                }
            )
            self._last_error = "auth_not_established"
            self._last_error_details = details
            _LOGGER.error("Umnye Seti request failed: %s", self.format_error_details(details))
            return {
                "error": "auth_failed",
                "message": "Сервер не создал авторизованную сессию после входа",
            }

        return {
            "error": "cannot_connect",
            "message": str(message or error_code or "Authorization verification failed"),
        }

    async def auth(self, login: str, password: str) -> dict[str, Any]:
        """Authenticate and prove the session by fetching account JSON.

        The cabinet is Rails-like and may answer the login POST either with JSON
        (AJAX flow) or with an HTTP redirect (normal browser flow). Both are
        supported; neither is considered successful until the authenticated JSON
        endpoint is verified.
        """
        self._clear_error()
        self._set_sensitive(login, password)
        self._clear_cookies()

        try:
            _LOGGER.debug("Umnye Seti auth stage=auth.init: GET %s", INIT_URL)
            init_headers = self._headers_html()
            try:
                async with self._session.get(
                    INIT_URL,
                    headers=init_headers,
                    ssl=self._verify_ssl,
                    timeout=DEFAULT_TIMEOUT,
                ) as resp:
                    html = await resp.text(errors="replace")
                    self._trace_response("auth.init", resp, html)
                    self._record_raw(
                        "auth.init", method="GET", url=INIT_URL,
                        request_headers=init_headers, resp=resp, response_text=html
                    )

                    if resp.status >= 400:
                        self._set_error(
                            "auth_init_http_error",
                            "auth.init",
                            f"HTTP {resp.status} while opening login page",
                            resp=resp,
                            text=html,
                        )
                        return {"error": "cannot_connect", "message": self._last_error_details["message"]}

                    init_url = str(resp.url)
                    login_form = self._parse_login_page(html)
                    token = login_form.get("token")
                    self._add_sensitive(token)

                    if not token:
                        self._set_error(
                            "init_token_not_found",
                            "auth.token",
                            "Authorization token was not found on the login page",
                            resp=resp,
                            text=html,
                            extra={"login_form_found": bool(login_form.get("form_found"))},
                        )
                        return {"error": "auth_failed", "message": "Не найден CSRF/authenticity token формы входа"}
            except (asyncio.TimeoutError, ClientError) as exc:
                self._record_raw("auth.init", method="GET", url=INIT_URL, request_headers=init_headers, exc=exc)
                self._set_error(
                    "auth_init_network_error",
                    "auth.init",
                    "Failed to open the login page",
                    exc=exc,
                )
                return {"error": "cannot_connect", "message": str(exc) or type(exc).__name__}
            except Exception as exc:
                self._record_raw("auth.init", method="GET", url=INIT_URL, request_headers=init_headers, exc=exc)
                self._set_error(
                    "auth_init_exception",
                    "auth.init",
                    "Unexpected error while opening the login page",
                    exc=exc,
                )
                return {"error": "cannot_connect", "message": str(exc) or type(exc).__name__}

            action = str(login_form.get("action") or AUTH_URL)
            auth_url = urljoin(init_url, action)
            if not self._is_safe_auth_url(auth_url):
                self._set_error(
                    "unsafe_auth_action",
                    "auth.form",
                    "Login form points to an unexpected host or non-HTTPS URL",
                    extra={"form_action": auth_url},
                )
                return {"error": "cannot_connect", "message": "Форма входа указывает на неожиданный адрес"}

            method = str(login_form.get("method") or "post").lower()
            if method != "post":
                self._set_error(
                    "unsupported_auth_method",
                    "auth.form",
                    f"Unsupported login form method: {method}",
                    extra={"form_action": auth_url},
                )
                return {"error": "cannot_connect", "message": f"Неподдерживаемый метод формы входа: {method}"}

            form = dict(login_form.get("fields") or {})
            form["user[login]"] = login
            form["user[password]"] = password
            form["authenticity_token"] = str(token)
            # Rails-generated forms traditionally include utf8=✓. The old code
            # sent the HTML entity literally (&#x2713;), which is not what a
            # browser submits.
            form["utf8"] = form.get("utf8") or "✓"
            form.setdefault("commit", "Войти")

            _LOGGER.debug(
                "Umnye Seti auth stage=auth.form: action=%s; method=POST; form_fields=%s; token_source=%s",
                auth_url,
                sorted(form.keys()),
                "form/meta",
            )

            auth_headers = self._headers_form(init_url, str(token))
            try:
                # Do not auto-follow here: the first Location is useful to
                # distinguish a normal successful browser redirect from a bounce
                # back to /login.
                async with self._session.post(
                    auth_url,
                    headers=auth_headers,
                    ssl=self._verify_ssl,
                    data=form,
                    timeout=DEFAULT_TIMEOUT,
                    allow_redirects=False,
                ) as resp:
                    text = await resp.text(errors="replace")
                    self._trace_response("auth.submit", resp, text)
                    self._record_raw(
                        "auth.submit", method="POST", url=auth_url,
                        request_headers=auth_headers, request_body=form,
                        resp=resp, response_text=text
                    )
                    await self._persist()

                    if resp.status in (401, 403, 422):
                        message = self._extract_html_error(text) or f"HTTP {resp.status} during authorization"
                        try:
                            payload = _json.loads(text)
                        except (TypeError, ValueError):
                            payload = None
                        if isinstance(payload, dict):
                            message = self._auth_failure_from_json(payload) or str(payload.get("message") or message)
                        self._set_error("auth_failed", "auth.submit", message, resp=resp, text=text)
                        return {"error": "auth_failed", "message": message}

                    if resp.status >= 400:
                        message = self._extract_html_error(text) or f"HTTP {resp.status} during authorization"
                        self._set_error("auth_submit_http_error", "auth.submit", message, resp=resp, text=text)
                        return {"error": "cannot_connect", "message": message}

                    if 300 <= resp.status < 400:
                        location = resp.headers.get("Location")
                        if not location:
                            self._set_error(
                                "auth_redirect_without_location",
                                "auth.submit",
                                "Login endpoint returned a redirect without Location header",
                                resp=resp,
                                text=text,
                            )
                            return {"error": "auth_failed", "message": "Сервер вернул редирект без адреса назначения"}

                        redirect_url = urljoin(auth_url, location)
                        if self._is_login_url(redirect_url):
                            message = self._extract_html_error(text) or "Authorization redirected back to the login page"
                            self._set_error(
                                "auth_failed",
                                "auth.submit",
                                message,
                                resp=resp,
                                text=text,
                                extra={"redirect_url": redirect_url},
                            )
                            return {"error": "auth_failed", "message": message}

                        _LOGGER.debug(
                            "Umnye Seti auth stage=auth.submit: browser-style redirect accepted as candidate success; redirect_url=%s",
                            redirect_url,
                        )
                        return await self._verify_auth()

                    try:
                        payload = _json.loads(text) if text.strip() else {}
                    except (TypeError, ValueError):
                        payload = None

                    if isinstance(payload, dict):
                        failure = self._auth_failure_from_json(payload)
                        if failure:
                            self._set_error("auth_failed", "auth.submit", failure, resp=resp, text=text)
                            return {"error": "auth_failed", "message": failure}

                        _LOGGER.debug(
                            "Umnye Seti auth stage=auth.submit: JSON response accepted as candidate success; keys=%s",
                            sorted(str(k) for k in payload.keys()),
                        )
                        return await self._verify_auth(payload)

                    html_error = self._extract_html_error(text)
                    if html_error:
                        self._set_error("auth_failed", "auth.submit", html_error, resp=resp, text=text)
                        return {"error": "auth_failed", "message": html_error}

                    if self._looks_like_login_page(resp, text):
                        message = "Сервер вернул страницу входа после отправки логина и пароля"
                        self._set_error("auth_failed", "auth.submit", message, resp=resp, text=text)
                        return {"error": "auth_failed", "message": message}

                    # A 2xx HTML/empty response can be a perfectly valid normal
                    # form login. Verify the resulting cookies instead of guessing.
                    _LOGGER.debug(
                        "Umnye Seti auth stage=auth.submit: non-JSON 2xx response accepted as candidate success; verifying session"
                    )
                    return await self._verify_auth()
            except (asyncio.TimeoutError, ClientError) as exc:
                self._record_raw(
                    "auth.submit", method="POST", url=auth_url,
                    request_headers=auth_headers, request_body=form, exc=exc
                )
                self._set_error(
                    "auth_submit_network_error",
                    "auth.submit",
                    "Authorization request failed",
                    exc=exc,
                )
                return {"error": "cannot_connect", "message": str(exc) or type(exc).__name__}
            except Exception as exc:
                self._record_raw(
                    "auth.submit", method="POST", url=auth_url,
                    request_headers=auth_headers, request_body=form, exc=exc
                )
                self._set_error(
                    "auth_submit_exception",
                    "auth.submit",
                    "Unexpected authorization error",
                    exc=exc,
                )
                return {"error": "cannot_connect", "message": str(exc) or type(exc).__name__}
        finally:
            # Error snippets have already been sanitized; do not retain credentials
            # or CSRF tokens in the API object longer than necessary.
            self._clear_sensitive()

    async def _fetch_json(
        self,
        *,
        stage: str,
        clear_error: bool,
        unauthorized_level: int = logging.WARNING,
    ) -> dict[str, Any]:
        if clear_error:
            self._clear_error()

        _LOGGER.debug("Umnye Seti stage=%s: GET %s", stage, INIT_URL)
        request_headers = self._headers_json()
        try:
            async with self._session.get(
                INIT_URL,
                headers=request_headers,
                ssl=self._verify_ssl,
                timeout=DEFAULT_TIMEOUT,
            ) as resp:
                text = await resp.text(errors="replace")
                self._trace_response(stage, resp, text)
                self._record_raw(
                    stage, method="GET", url=INIT_URL,
                    request_headers=request_headers, resp=resp, response_text=text
                )
                await self._persist()

                if resp.status in (401, 403) or self._looks_like_login_page(resp, text):
                    self._set_error(
                        "unauthorized",
                        stage,
                        "Session is not authorized or has expired",
                        resp=resp,
                        text=text,
                        level=unauthorized_level,
                    )
                    return {"error": "unauthorized", "message": "Сессия не авторизована или истекла"}

                if resp.status >= 400:
                    self._set_error(
                        "fetch_http_error",
                        stage,
                        f"HTTP {resp.status} while requesting account data",
                        resp=resp,
                        text=text,
                    )
                    return {"error": "cannot_connect", "message": self._last_error_details["message"]}

                try:
                    payload = _json.loads(text)
                except (TypeError, ValueError) as exc:
                    self._set_error(
                        "invalid_json",
                        f"{stage}.parse",
                        "Server returned a non-JSON response",
                        resp=resp,
                        text=text,
                        exc=exc,
                    )
                    return {"error": "invalid_json", "message": "Сервер вернул ответ не в формате JSON"}

                if not isinstance(payload, dict):
                    self._set_error(
                        "invalid_response_type",
                        f"{stage}.parse",
                        f"Expected JSON object, got {type(payload).__name__}",
                        resp=resp,
                        text=text,
                    )
                    return {"error": "invalid_response", "message": "Некорректный формат ответа сервера"}

                if payload.get("error"):
                    message = str(payload.get("message") or payload.get("error"))
                    self._set_error(
                        "server_error",
                        stage,
                        message,
                        resp=resp,
                        text=text,
                    )
                    return payload

                self._clear_error()
                return payload
        except (asyncio.TimeoutError, ClientError) as exc:
            self._record_raw(stage, method="GET", url=INIT_URL, request_headers=request_headers, exc=exc)
            self._set_error(
                "fetch_network_error",
                stage,
                "Failed to request account data",
                exc=exc,
            )
            return {"error": "cannot_connect", "message": str(exc) or type(exc).__name__}
        except Exception as exc:
            self._record_raw(stage, method="GET", url=INIT_URL, request_headers=request_headers, exc=exc)
            self._set_error(
                "fetch_exception",
                stage,
                "Unexpected error while requesting account data",
                exc=exc,
            )
            return {"error": "cannot_connect", "message": str(exc) or type(exc).__name__}

    async def fetch_json(self) -> dict[str, Any]:
        """Fetch account JSON and distinguish auth expiry from server/data errors."""
        return await self._fetch_json(stage="data.fetch", clear_error=True)
