from __future__ import annotations
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .coordinator import UmnyeSetiCoordinator

PLATFORMS = [Platform.SENSOR]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    integration = await async_get_integration(hass, entry.domain)
    version = integration.version
    cfg = {**entry.data, **entry.options, "entry_id": entry.entry_id, "version": version}
    coordinator = UmnyeSetiCoordinator(hass, cfg)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    integration = await async_get_integration(hass, DOMAIN)
    hass.data.setdefault(DOMAIN, {})["manifest_version"] = integration.version

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
