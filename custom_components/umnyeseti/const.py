from __future__ import annotations

DOMAIN = "umnyeseti"

CONF_LOGIN = "login"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_PAYMENT_LINK_TOKEN = "payment_link_token"

DEFAULT_UPDATE_INTERVAL = 30  # minutes
MIN_UPDATE_INTERVAL = 15
DEFAULT_VERIFY_SSL = False

INIT_URL = "https://stat.umnyeseti.ru"
AUTH_URL = "https://stat.umnyeseti.ru/login"

USER_AGENT_TEMPLATE = "Mozilla/5.0 (compatible; SmartNetwork-HA/{version}; +https://github.com/DevWorkTech/ha-smartnetworks)"

CURRENCY = "RUB"

PAYMENT_BRIDGE_PATH = "/api/umnyeseti/payment/{entry_id}/{token}"
