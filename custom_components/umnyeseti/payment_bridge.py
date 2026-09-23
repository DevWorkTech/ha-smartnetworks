from __future__ import annotations

import asyncio
import html
import hmac
import json
import logging

from aiohttp import ClientError, ClientTimeout, web
from homeassistant.components.http import KEY_HASS
from homeassistant.components.http.view import HomeAssistantView

from .const import CONF_PAYMENT_LINK_TOKEN, DOMAIN
from .payment import (
    PAYMENT_BASE_URL,
    PAYMENT_CONFIRM_URL,
    format_payment_amount,
    parse_payment_confirmation,
)

_LOGGER = logging.getLogger(__name__)


class UmnyeSetiPaymentBridgeView(HomeAssistantView):
    """Token-protected payment bridge with a lightweight HA-style form."""

    url = "/api/umnyeseti/payment/{entry_id}/{token}"
    name = "api:umnyeseti:payment"
    requires_auth = False

    @staticmethod
    def _headers(*, script: bool = False) -> dict[str, str]:
        csp = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'"
        if script:
            csp += "; script-src 'unsafe-inline'"
        return {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": csp,
        }

    @staticmethod
    def _resolve(request: web.Request, entry_id: str, token: str):
        hass = request.app[KEY_HASS]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise web.HTTPNotFound()

        expected = str(entry.data.get(CONF_PAYMENT_LINK_TOKEN) or "")
        if not expected or not hmac.compare_digest(expected, str(token or "")):
            _LOGGER.warning(
                "%s: rejected payment bridge request: entry_id=%s; reason=invalid_token",
                DOMAIN,
                entry_id,
            )
            raise web.HTTPNotFound()

        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        state = getattr(coordinator, "data", None)
        mapped = getattr(state, "data", None) if state is not None else None
        if not isinstance(mapped, dict):
            return None, None
        return coordinator, mapped

    async def get(
        self, request: web.Request, entry_id: str, token: str
    ) -> web.StreamResponse:
        _coordinator, mapped = self._resolve(request, entry_id, token)
        if not isinstance(mapped, dict):
            return self._error_page()

        tariff = mapped.get("tariff") or {}
        account = str(mapped.get("account") or "").strip()
        amount = format_payment_amount(tariff.get("pay_subscribe"))
        if not account or amount is None:
            return self._error_page()

        return self._form_page(
            account=account,
            amount=amount,
            storage_key=f"umnyeseti_payment_started_{entry_id}",
        )

    async def post(
        self, request: web.Request, entry_id: str, token: str
    ) -> web.StreamResponse:
        _coordinator, mapped = self._resolve(request, entry_id, token)
        if not isinstance(mapped, dict):
            return self._error_page()

        try:
            posted = await request.post()
        except Exception as exc:
            _LOGGER.warning(
                "%s: payment bridge could not parse form: entry_id=%s; exception_type=%s; exception=%s",
                DOMAIN,
                entry_id,
                type(exc).__name__,
                exc,
            )
            return self._error_page()

        # Account is intentionally never trusted from the browser. The disabled
        # field is display-only; the actual account always comes from fresh API data.
        account = str(mapped.get("account") or "").strip()
        amount = format_payment_amount(posted.get("amount"))
        try:
            valid_positive_amount = amount is not None and float(amount) > 0
        except (TypeError, ValueError):
            valid_positive_amount = False

        if not account or not valid_positive_amount:
            return self._error_page("Укажите корректную сумму оплаты больше 0.")

        checkout_url, error_message = await self._confirm_payment(
            coordinator=_coordinator,
            entry_id=entry_id,
            account=account,
            amount=amount,
        )
        if not checkout_url:
            return self._error_page(error_message)

        # The external gateway is now known to be valid and the browser is
        # about to leave Home Assistant. Hide all active payment reminders before
        # returning the redirect page, so persistent and mobile notifications do
        # not remain visible after the user has already proceeded to payment.
        try:
            clear_method = getattr(_coordinator, "async_payment_gateway_opened", None)
            if clear_method is not None:
                await clear_method()
        except Exception as exc:
            # Do not block the already-confirmed payment handoff just because a
            # notification service failed to clear. The coordinator logs the
            # per-service failures as well; keep this guard for robustness.
            _LOGGER.warning(
                "%s: payment gateway confirmed but reminder cleanup failed: entry_id=%s; exception_type=%s; exception=%s",
                DOMAIN,
                entry_id,
                type(exc).__name__,
                exc,
            )

        _LOGGER.info(
            "%s: payment gateway confirmed form: entry_id=%s; amount=%s; redirect_host=yoomoney.ru",
            DOMAIN,
            entry_id,
            amount,
        )
        return self._redirect_page(
            checkout_url,
            storage_key=f"umnyeseti_payment_started_{entry_id}",
        )

    @staticmethod
    async def _confirm_payment(*, coordinator, entry_id: str, account: str, amount: str) -> tuple[str | None, str | None]:
        """POST the validated form to Smart Networks and extract the generated YooMoney URL."""
        form = {
            "account": account,
            "amount": amount,
            "mode": "",
            "user_account": account,
            "locale": "",
        }
        headers = {
            "User-Agent": getattr(getattr(coordinator, "api", None), "user_agent", "Mozilla/5.0"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://sberpay.umnyeseti.ru",
            "Referer": PAYMENT_BASE_URL,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        timeout = ClientTimeout(total=20, connect=10, sock_read=15)
        session = getattr(coordinator, "session", None)
        api = getattr(coordinator, "api", None)
        verify_ssl = bool(getattr(coordinator, "_verify_ssl", False))
        if session is None:
            return None, "Платёжный сервис временно недоступен. Повторите попытку позже."

        try:
            async with session.post(
                PAYMENT_CONFIRM_URL,
                data=form,
                headers=headers,
                ssl=verify_ssl,
                timeout=timeout,
                allow_redirects=True,
            ) as resp:
                response_text = await resp.text(errors="replace")
                if api is not None and hasattr(api, "record_raw_http"):
                    api.record_raw_http(
                        "payment.confirm",
                        method="POST",
                        url=PAYMENT_CONFIRM_URL,
                        request_headers=headers,
                        request_body=form,
                        resp=resp,
                        response_text=response_text,
                    )

                if resp.status < 200 or resp.status >= 300:
                    _LOGGER.warning(
                        "%s: payment confirm failed: entry_id=%s; http_status=%s",
                        DOMAIN, entry_id, resp.status,
                    )
                    return None, "Платёжный сервис вернул ошибку. Повторите попытку позже."

                success, checkout_url = parse_payment_confirmation(response_text)
                if not success:
                    _LOGGER.warning(
                        "%s: payment confirm rejected or success marker missing: entry_id=%s",
                        DOMAIN, entry_id,
                    )
                    return None, "Платёжный сервис не подтвердил введённые данные. Проверьте сумму и повторите попытку."
                if not checkout_url:
                    _LOGGER.warning(
                        "%s: payment confirm succeeded but YooMoney link is missing: entry_id=%s",
                        DOMAIN, entry_id,
                    )
                    return None, "Данные подтверждены, но ссылка оплаты не получена. Повторите попытку позже."
                return checkout_url, None
        except (asyncio.TimeoutError, ClientError) as exc:
            if api is not None and hasattr(api, "record_raw_http"):
                api.record_raw_http(
                    "payment.confirm",
                    method="POST",
                    url=PAYMENT_CONFIRM_URL,
                    request_headers=headers,
                    request_body=form,
                    exc=exc,
                )
            _LOGGER.warning(
                "%s: payment confirm network error: entry_id=%s; exception_type=%s; exception=%s",
                DOMAIN, entry_id, type(exc).__name__, exc,
            )
            return None, "Не удалось связаться с платёжным сервисом. Повторите попытку позже."
        except Exception as exc:
            if api is not None and hasattr(api, "record_raw_http"):
                api.record_raw_http(
                    "payment.confirm",
                    method="POST",
                    url=PAYMENT_CONFIRM_URL,
                    request_headers=headers,
                    request_body=form,
                    exc=exc,
                )
            _LOGGER.exception(
                "%s: unexpected payment confirm error: entry_id=%s; exception_type=%s",
                DOMAIN, entry_id, type(exc).__name__,
            )
            return None, "Произошла ошибка оплаты. Повторите попытку позже."

    @classmethod
    def _form_page(cls, *, account: str, amount: str, storage_key: str) -> web.Response:
        account_html = html.escape(account, quote=True)
        amount_html = html.escape(amount, quote=True)
        key_js = json.dumps(storage_key)
        page = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Умные Сети — Оплата</title>
<style>
:root{{--primary:#03a9f4;--primary-hover:#0398dc;--bg:#f4f6f8;--surface:#ffffff;--text:#1f1f1f;--muted:#6f7782;--line:#d9dde3;--disabled:#f1f3f5;--disabled-text:#555d66;--shadow:0 8px 26px rgba(0,0,0,.10)}}
*{{box-sizing:border-box}}
html,body{{margin:0;min-height:100%;background:var(--bg);color:var(--text);font:400 16px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}}
body{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:22px 16px}}
.wrap{{width:min(100%,430px)}}
.brand{{display:flex;align-items:center;gap:12px;margin:0 2px 15px}}
.logo{{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;background:var(--primary);color:#fff;font-size:21px;font-weight:700;box-shadow:0 4px 14px rgba(3,169,244,.28)}}
.brand-title{{font-size:20px;font-weight:600;line-height:1.2;letter-spacing:.1px}}
.brand-sub{{margin-top:3px;color:var(--muted);font-size:13px}}
.card{{background:var(--surface);border:1px solid rgba(0,0,0,.06);border-radius:16px;box-shadow:var(--shadow);overflow:hidden}}
.top{{padding:23px 23px 13px}}
h1{{margin:0;font-size:23px;line-height:1.25;font-weight:600}}
.intro{{margin:7px 0 0;color:var(--muted);font-size:14px}}
form{{padding:8px 23px 23px}}
.field{{margin:0 0 17px}}
label{{display:block;margin:0 0 7px;color:var(--muted);font-size:13px;font-weight:500}}
.input-wrap,.select-wrap{{position:relative}}
input,select{{width:100%;height:50px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--text);padding:0 14px;font:inherit;outline:none;transition:border-color .15s,box-shadow .15s,background .15s}}
input:focus{{border-color:var(--primary);box-shadow:0 0 0 2px rgba(3,169,244,.13)}}
input:disabled,select:disabled{{background:var(--disabled);color:var(--disabled-text);opacity:1;-webkit-text-fill-color:var(--disabled-text);cursor:not-allowed}}
select{{appearance:none;-webkit-appearance:none;padding-right:42px}}
.select-wrap:after{{content:"▾";position:absolute;right:15px;top:13px;color:#9aa1a9;font-size:16px;pointer-events:none}}
.amount input{{padding-right:44px}}
.currency{{position:absolute;right:15px;top:13px;color:var(--muted);pointer-events:none}}
button{{width:100%;min-height:50px;margin-top:3px;border:0;border-radius:10px;background:var(--primary);color:#fff;font:600 16px system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;cursor:pointer;box-shadow:0 3px 10px rgba(3,169,244,.25);transition:background .15s,transform .05s,opacity .15s}}
button:hover{{background:var(--primary-hover)}}button:active{{transform:translateY(1px)}}button:disabled{{opacity:.65;cursor:default;transform:none}}
.note{{padding:15px 23px 18px;border-top:1px solid var(--line);color:var(--muted);font-size:12px;text-align:center;background:rgba(0,0,0,.012)}}
.done{{display:none;padding:34px 25px;text-align:center}}
.done-icon{{width:58px;height:58px;margin:0 auto 15px;border-radius:50%;display:grid;place-items:center;background:rgba(3,169,244,.12);color:var(--primary);font-size:28px;font-weight:600}}
.done h2{{margin:0 0 8px;font-size:21px;font-weight:600}}.done p{{margin:0;color:var(--muted);font-size:14px}}
@media(max-width:420px){{body{{padding:16px 12px}}.top{{padding:21px 19px 12px}}form{{padding:8px 19px 20px}}.note{{padding:14px 19px 17px}}}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111416;--surface:#1d2023;--text:#f1f3f5;--muted:#a9b0b8;--line:#3b4147;--disabled:#2a2f34;--disabled-text:#c4c9cf;--shadow:0 8px 28px rgba(0,0,0,.42)}}.card{{border-color:#30363c}}.note{{background:rgba(255,255,255,.012)}}}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand"><div class="logo">⌂</div><div><div class="brand-title">Умные Сети</div><div class="brand-sub">Home Assistant</div></div></div>
  <div class="card">
    <div id="form-view">
      <div class="top"><h1>Оплата</h1><p class="intro">Проверьте лицевой счёт и сумму платежа</p></div>
      <form id="payment-form" method="post" autocomplete="off">
        <div class="field"><label for="account">Лицевой счёт</label><input id="account" value="{account_html}" disabled></div>
        <div class="field"><label for="payment-method">Способ оплаты</label><div class="select-wrap"><select id="payment-method" disabled aria-disabled="true"><option selected>Оплата 🏦 СБП / 💳 Картой</option></select></div></div>
        <div class="field"><label for="amount">Сумма оплаты</label><div class="input-wrap amount"><input id="amount" name="amount" type="number" inputmode="decimal" min="0.01" step="0.01" value="{amount_html}" required><span class="currency">₽</span></div></div>
        <button id="submit" type="submit">💳 Оплатить</button>
      </form>
      <div class="note">После нажатия откроется защищённая страница оплаты</div>
    </div>
    <div class="done" id="done"><div class="done-icon">✓</div><h2>Оплата уже открыта</h2><p>Повторная отправка формы заблокирована. Продолжите оплату на уже открытой странице.</p></div>
  </div>
</div>
<script>
(function(){{
  const key={key_js};
  const formView=document.getElementById('form-view');
  const form=document.getElementById('payment-form');
  const done=document.getElementById('done');
  const nav=performance.getEntriesByType('navigation')[0];
  function showDone(){{formView.style.display='none';done.style.display='block';}}
  function isBack(){{return !!(nav && nav.type==='back_forward');}}
  if(isBack() && sessionStorage.getItem(key)==='1'){{showDone();}}
  else if(!isBack()){{sessionStorage.removeItem(key);}}
  window.addEventListener('pageshow',function(e){{if((e.persisted||isBack())&&sessionStorage.getItem(key)==='1')showDone();}});
  form.addEventListener('submit',function(){{
    const b=document.getElementById('submit');
    b.disabled=true;b.textContent='Открываем оплату…';
  }});
}})();
</script>
</body>
</html>"""
        return web.Response(
            text=page,
            content_type="text/html",
            charset="utf-8",
            headers=cls._headers(script=True),
        )

    @classmethod
    def _redirect_page(cls, payment_url: str, *, storage_key: str) -> web.Response:
        url_js = json.dumps(payment_url)
        url_html = html.escape(payment_url, quote=True)
        key_js = json.dumps(storage_key)
        page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="color-scheme" content="light dark"><title>Переход к оплате</title>
<style>:root{{--primary:#03a9f4;--bg:#f5f5f5;--surface:#fff;--text:#212121;--muted:#757575}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;background:var(--bg);color:var(--text);font:16px system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}}.c{{width:min(100%,420px);background:var(--surface);border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.14);padding:30px 24px;text-align:center}}.i{{width:54px;height:54px;margin:0 auto 14px;border-radius:50%;display:grid;place-items:center;background:rgba(3,169,244,.12);color:var(--primary);font-size:26px}}h1{{font-size:20px;font-weight:500;margin:0 0 8px}}p{{margin:0;color:var(--muted);font-size:14px}}a{{display:inline-block;margin-top:18px;color:var(--primary);text-decoration:none;font-weight:500}}@media(prefers-color-scheme:dark){{:root{{--bg:#111416;--surface:#1c1c1c;--text:#eee;--muted:#aaa}}}}</style></head>
<body><div class="c"><div class="i">↗</div><h1>Открываем страницу оплаты…</h1><p>Переход произойдёт автоматически.</p><a href="{url_html}">💳 Перейти к оплате</a></div>
<script>history.replaceState(null,document.title,location.href);sessionStorage.setItem({key_js},'1');window.setTimeout(function(){{location.replace({url_js});}},30);</script>
</body></html>"""
        return web.Response(
            text=page,
            content_type="text/html",
            charset="utf-8",
            headers=cls._headers(script=True),
        )

    @classmethod
    def _error_page(cls, message: str | None = None, status: int = 400) -> web.Response:
        safe = html.escape(message or "Произошла ошибка. Повторите оплату позже.")
        page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="color-scheme" content="light dark"><title>Умные Сети — Ошибка оплаты</title>
<style>:root{{--primary:#03a9f4;--bg:#f5f5f5;--surface:#fff;--text:#212121;--muted:#757575;--danger:#d32f2f}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;background:var(--bg);color:var(--text);font:16px system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}}.c{{width:min(100%,420px);background:var(--surface);border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.14);padding:30px 24px;text-align:center}}.i{{width:54px;height:54px;margin:0 auto 14px;border-radius:50%;display:grid;place-items:center;background:rgba(211,47,47,.10);color:var(--danger);font-size:26px}}h1{{font-size:20px;font-weight:500;margin:0 0 8px}}p{{margin:0;color:var(--muted);font-size:14px}}@media(prefers-color-scheme:dark){{:root{{--bg:#111416;--surface:#1c1c1c;--text:#eee;--muted:#aaa}}}}</style></head>
<body><div class="c"><div class="i">!</div><h1>Ошибка оплаты</h1><p>{safe}</p></div></body></html>"""
        return web.Response(
            text=page,
            status=status,
            content_type="text/html",
            charset="utf-8",
            headers=cls._headers(),
        )
