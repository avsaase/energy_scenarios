"""Energy Scenarios integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, INTERVALS, SELECTED_SENSORS

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


def get_entry_config(entry: ConfigEntry) -> dict[str, Any]:
    """Return merged config entry data and options."""
    return {**entry.data, **entry.options}


def get_selected_sensors(entry: ConfigEntry) -> set[str]:
    """Return the set of selected interval keys."""
    selected = entry.options.get(SELECTED_SENSORS)
    if selected is None:
        selected = entry.data.get(SELECTED_SENSORS)
    if selected is None:
        return set(INTERVALS)
    return set(selected)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Energy Scenarios from a config entry."""
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _LOGGER.info("Setting up Energy Scenarios: %s", get_entry_config(entry))

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception as e:
        _LOGGER.error("Failed to set up sensor platform: %s", e)
        return False

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    config = get_entry_config(entry)
    title = f"Energy Scenarios — {config.get('name', 'Unnamed')}"
    if entry.title != title:
        hass.config_entries.async_update_entry(entry, title=title)
    await hass.config_entries.async_reload(entry.entry_id)
