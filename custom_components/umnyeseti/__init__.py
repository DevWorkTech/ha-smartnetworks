from __future__ import annotations
import secrets
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.loader import async_get_integration

from .const import DOMAIN, CONF_PAYMENT_LINK_TOKEN
from .payment_bridge import UmnyeSetiPaymentBridgeView
from .coordinator import UmnyeSetiCoordinator

PLATFORMS = [Platform.SENSOR]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    # Existing installations created before 1.0.16 do not have a bridge token.
    # Generate it once and persist it in the config entry, just like the Yonet
    # payment bridge. It is never regenerated on reload.
    entry_data = dict(entry.data)
    if not entry_data.get(CONF_PAYMENT_LINK_TOKEN):
        entry_data[CONF_PAYMENT_LINK_TOKEN] = secrets.token_urlsafe(32)
        hass.config_entries.async_update_entry(entry, data=entry_data)

    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get("payment_bridge_registered"):
        hass.http.register_view(UmnyeSetiPaymentBridgeView())
        domain_data["payment_bridge_registered"] = True

    integration = await async_get_integration(hass, entry.domain)
    version = integration.version
    cfg = {**entry_data, **entry.options, "entry_id": entry.entry_id, "version": version}
    coordinator = UmnyeSetiCoordinator(hass, cfg)
    domain_data[entry.entry_id] = coordinator

    integration = await async_get_integration(hass, DOMAIN)
    domain_data["manifest_version"] = integration.version

    entry.async_on_unload(entry.add_update_listener(async_options_updated))

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coord: UmnyeSetiCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coord.async_close()
    return unload_ok

async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)
