from __future__ import annotations

DOMAIN = "umnyeseti"

CONF_LOGIN = "login"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
CONF_UPDATE_INTERVAL = "update_interval"

DEFAULT_UPDATE_INTERVAL = 15  # minutes
MIN_UPDATE_INTERVAL = 15

INIT_URL = "https://stat.umnyeseti.ru"
AUTH_URL = "https://stat.umnyeseti.ru/login"

USER_AGENT_TEMPLATE = "Mozilla/5.0 (compatible; SmartNetwork-HA/{version}; +https://github.com/DevWorkTech/ha-smartnetworks)"

CURRENCY = "RUB"
