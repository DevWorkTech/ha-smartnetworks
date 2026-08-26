from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode

PAYMENT_BASE_URL = "https://sberpay.umnyeseti.ru/ya_kassa/"


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


def build_payment_url(account_id, payment_left) -> str | None:
    """Build the external Smart Networks payment URL safely."""
    account = str(account_id or "").strip()
    amount = format_payment_amount(payment_left)
    if not account or amount is None:
        return None

    return f"{PAYMENT_BASE_URL}?{urlencode({'account': account, 'amount': amount})}"
