from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

PAYMENT_BASE_URL = "https://sberpay.umnyeseti.ru/ya_kassa/"
PAYMENT_CONFIRM_URL = "https://sberpay.umnyeseti.ru/ya_kassa/confirm"
PAYMENT_SUCCESS_TEXT = "Проверка введенных данных прошла успешно"
PAYMENT_BRIDGE_TEMPLATE = "/api/umnyeseti/payment/{entry_id}/{token}"


def format_payment_amount(value) -> str | None:
    """Return payment amount with exactly two decimal places for the gateway."""
    if value is None:
        return None

    try:
        if isinstance(value, str):
            normalized = value.strip().replace(" ", "").replace(",", ".")
        else:
            normalized = str(value)
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError, TypeError):
        return None

    if not amount.is_finite() or amount < 0:
        return None

    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def build_payment_bridge_path(entry_id, token) -> str | None:
    """Build the local Home Assistant bridge path for a payment link."""
    entry = str(entry_id or "").strip()
    secret = str(token or "").strip()
    if not entry or not secret:
        return None
    return PAYMENT_BRIDGE_TEMPLATE.format(
        entry_id=quote(entry, safe=""),
        token=quote(secret, safe=""),
    )


class _PaymentConfirmParser(HTMLParser):
    """Extract the success marker and generated YooMoney checkout link."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._success_depth = 0
        self._success_parts: list[str] = []
        self.checkout_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {str(k).lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        classes = {x for x in attrs_dict.get("class", "").split() if x}

        if tag == "div" and {"alert", "alert-success"}.issubset(classes):
            self._success_depth = 1
            return

        if self._success_depth and tag == "div":
            self._success_depth += 1

        if tag == "a" and {"btn", "btn-primary"}.issubset(classes):
            href = attrs_dict.get("href", "").strip()
            if href:
                self.checkout_links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if self._success_depth and tag.lower() == "div":
            self._success_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._success_depth:
            self._success_parts.append(data)

    @property
    def success_text(self) -> str:
        return " ".join(" ".join(self._success_parts).split())


def is_safe_yoomoney_url(url: str | None) -> bool:
    """Accept only HTTPS checkout links on YooMoney's own domain."""
    if not url:
        return False
    try:
        parsed = urlparse(str(url).strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme.lower() == "https" and (host == "yoomoney.ru" or host.endswith(".yoomoney.ru"))


def parse_payment_confirmation(html_text: str) -> tuple[bool, str | None]:
    """Return (success marker present, safe generated YooMoney URL)."""
    parser = _PaymentConfirmParser()
    try:
        parser.feed(str(html_text or ""))
        parser.close()
    except Exception:
        return False, None

    success = PAYMENT_SUCCESS_TEXT in parser.success_text
    if not success:
        return False, None

    for href in parser.checkout_links:
        if is_safe_yoomoney_url(href):
            return True, href
    return True, None
