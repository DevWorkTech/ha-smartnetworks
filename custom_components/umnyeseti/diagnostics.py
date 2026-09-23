from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_PASSWORD,
    CONF_PAYMENT_LINK_TOKEN,
    DOMAIN,
)
from .coordinator import UmnyeSetiCoordinator

# Home Assistant's standard diagnostics download is the only public RAW debug
# surface. HTTP RAW capture itself is always enabled in UmnyeSetiApi.
TO_REDACT = {
    CONF_PASSWORD,
    CONF_PAYMENT_LINK_TOKEN,
}


def _state_to_dict(coordinator: UmnyeSetiCoordinator) -> dict[str, Any] | None:
    state = coordinator.data
    if state is None:
        return None
    try:
        return asdict(state)
    except TypeError:
        return {
            "data": getattr(state, "data", None),
            "error": getattr(state, "error", None),
            "last_attempt": getattr(state, "last_attempt", None),
            "error_details": getattr(state, "error_details", None),
        }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return always-on sanitized diagnostics for a config entry.

    The API keeps the latest RAW request/response for each important stage in
    memory at all times. Passwords, cookies, authorization headers, CSRF/
    authenticity tokens and other token-like values are already sanitized by
    UmnyeSetiApi before reaching ``raw_debug``. Config-entry secrets are
    redacted again here as a second safety layer.
    """
    coordinator: UmnyeSetiCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )

    diagnostics: dict[str, Any] = {
        "integration": {
            "domain": DOMAIN,
            "version": hass.data.get(DOMAIN, {}).get("manifest_version"),
            "entry_id": entry.entry_id,
            "title": entry.title,
        },
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
    }

    if coordinator is None:
        diagnostics["coordinator"] = None
        diagnostics["raw_http"] = {}
        diagnostics["note"] = "Coordinator is not loaded; RAW HTTP data is unavailable."
        return diagnostics

    diagnostics.update(
        {
            "coordinator": {
                "update_interval_seconds": (
                    coordinator.update_interval.total_seconds()
                    if coordinator.update_interval is not None
                    else None
                ),
                "verify_ssl": getattr(coordinator, "_verify_ssl", None),
                "state": _state_to_dict(coordinator),
            },
            "api": {
                "last_error": coordinator.api.last_error,
                "last_error_details": coordinator.api.last_error_details,
            },
            # Full latest sanitized RAW request/response per stage. This is kept
            # permanently in memory and does not depend on logger level.
            "raw_http": coordinator.api.raw_debug,
        }
    )
    return diagnostics
