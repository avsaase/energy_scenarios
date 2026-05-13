"""Config flow for Energy Scenarios."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.components.input_number import DOMAIN as INPUT_NUMBER_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.helpers import selector
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.schema_config_entry_flow import SchemaFlowError
import voluptuous as vol

from .const import (
    BATTERY_CHARGE_SENSOR,
    BATTERY_DISCHARGE_SENSOR,
    DOMAIN,
    FEED_PRICE_SENSOR,
    GRID_EXPORT_SENSOR,
    GRID_IMPORT_SENSOR,
    INTERVALS,
    SELECTED_SENSORS,
    SENSOR_LABELS,
    SOLAR_PRODUCTION_SENSOR,
    TAKE_PRICE_SENSOR,
)
from . import get_selected_sensors

_LOGGER = logging.getLogger(__name__)

_PRICE_DOMAINS = [SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN, NUMBER_DOMAIN]


def _entity_selector(domains: list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domains, multiple=False)
    )


def _energy_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=[SENSOR_DOMAIN],
            multiple=False,
            filter=[{"domain": [SENSOR_DOMAIN], "device_class": ["energy"]}],
        )
    )


def _clean(value: Any) -> Any:
    return None if value in (None, "") else value


def _validate_sensors(data: dict[str, Any]) -> dict[str, Any]:
    """Validate sensor inputs and return cleaned data."""
    cv.entity_id(data[GRID_IMPORT_SENSOR])

    for key in (
        GRID_EXPORT_SENSOR,
        SOLAR_PRODUCTION_SENSOR,
        BATTERY_CHARGE_SENSOR,
        BATTERY_DISCHARGE_SENSOR,
    ):
        if data.get(key):
            cv.entity_id(data[key])

    charge = _clean(data.get(BATTERY_CHARGE_SENSOR))
    discharge = _clean(data.get(BATTERY_DISCHARGE_SENSOR))
    if bool(charge) != bool(discharge):
        raise SchemaFlowError("battery_sensors_incomplete")

    return {
        "name": data.get("name", "Unnamed"),
        GRID_IMPORT_SENSOR: data[GRID_IMPORT_SENSOR],
        GRID_EXPORT_SENSOR: _clean(data.get(GRID_EXPORT_SENSOR)),
        SOLAR_PRODUCTION_SENSOR: _clean(data.get(SOLAR_PRODUCTION_SENSOR)),
        BATTERY_CHARGE_SENSOR: charge,
        BATTERY_DISCHARGE_SENSOR: discharge,
    }


def _validate_prices(data: dict[str, Any]) -> dict[str, Any]:
    cv.entity_id(data[TAKE_PRICE_SENSOR])
    feed = _clean(data.get(FEED_PRICE_SENSOR))
    if feed:
        cv.entity_id(feed)
    return {
        TAKE_PRICE_SENSOR: data[TAKE_PRICE_SENSOR],
        FEED_PRICE_SENSOR: feed,
    }


def _sensor_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                "name", default=d.get("name", "Unnamed")
            ): selector.TextSelector(),
            vol.Required(
                GRID_IMPORT_SENSOR, default=d.get(GRID_IMPORT_SENSOR, vol.UNDEFINED)
            ): _energy_selector(),
            vol.Optional(
                GRID_EXPORT_SENSOR, default=d.get(GRID_EXPORT_SENSOR, vol.UNDEFINED)
            ): vol.Any(None, _energy_selector()),
            vol.Optional(
                SOLAR_PRODUCTION_SENSOR,
                default=d.get(SOLAR_PRODUCTION_SENSOR, vol.UNDEFINED),
            ): vol.Any(None, _energy_selector()),
            vol.Optional(
                BATTERY_CHARGE_SENSOR,
                default=d.get(BATTERY_CHARGE_SENSOR, vol.UNDEFINED),
            ): vol.Any(None, _energy_selector()),
            vol.Optional(
                BATTERY_DISCHARGE_SENSOR,
                default=d.get(BATTERY_DISCHARGE_SENSOR, vol.UNDEFINED),
            ): vol.Any(None, _energy_selector()),
        }
    )


def _price_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                TAKE_PRICE_SENSOR, default=d.get(TAKE_PRICE_SENSOR, vol.UNDEFINED)
            ): _entity_selector(_PRICE_DOMAINS),
            vol.Optional(
                FEED_PRICE_SENSOR, default=d.get(FEED_PRICE_SENSOR, vol.UNDEFINED)
            ): vol.Any(None, _entity_selector(_PRICE_DOMAINS)),
        }
    )


def _interval_schema(defaults: list[str] | None = None) -> vol.Schema:
    options = [
        selector.SelectOptionDict(value=k, label=SENSOR_LABELS[k]) for k in INTERVALS
    ]
    return vol.Schema(
        {
            vol.Required(
                SELECTED_SENSORS,
                default=defaults if defaults is not None else list(INTERVALS),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


class EnergyScenariosConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._data.update(_validate_sensors(user_input))
                return await self.async_step_prices()
            except SchemaFlowError as e:
                errors["base"] = str(e)
            except vol.Invalid:
                errors["base"] = "invalid_entity"

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _sensor_schema(), user_input
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_prices(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._data.update(_validate_prices(user_input))
                return await self.async_step_intervals()
            except SchemaFlowError as e:
                errors["base"] = str(e)
            except vol.Invalid:
                errors["base"] = "invalid_entity"

        return self.async_show_form(
            step_id="prices",
            data_schema=self.add_suggested_values_to_schema(
                _price_schema(), user_input
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_intervals(self, user_input=None):
        errors = {}
        if user_input is not None:
            selected = user_input.get(SELECTED_SENSORS, [])
            if not selected:
                errors["base"] = "no_sensors_selected"
            else:
                self._data[SELECTED_SENSORS] = selected
                return self.async_create_entry(
                    title=f"Energy Scenarios — {self._data.get('name', 'Unnamed')}",
                    data=self._data,
                )

        return self.async_show_form(
            step_id="intervals",
            data_schema=_interval_schema(),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return EnergyScenariosOptionsFlow(config_entry)


class EnergyScenariosOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        super().__init__()
        self._config_entry = config_entry
        self._data: dict[str, Any] = {}

    async def async_step_init(self, user_input=None):
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        errors = {}
        current = {**self._config_entry.data, **self._config_entry.options}

        if user_input is not None:
            try:
                self._data.update(_validate_sensors(user_input))
                return await self.async_step_prices()
            except SchemaFlowError as e:
                errors["base"] = str(e)
            except vol.Invalid:
                errors["base"] = "invalid_entity"

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _sensor_schema(current), user_input or current
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_prices(self, user_input=None):
        errors = {}
        current = {**self._config_entry.data, **self._config_entry.options}

        if user_input is not None:
            try:
                self._data.update(_validate_prices(user_input))
                return await self.async_step_intervals()
            except SchemaFlowError as e:
                errors["base"] = str(e)
            except vol.Invalid:
                errors["base"] = "invalid_entity"

        return self.async_show_form(
            step_id="prices",
            data_schema=self.add_suggested_values_to_schema(
                _price_schema(current), user_input or current
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_intervals(self, user_input=None):
        errors = {}
        if user_input is not None:
            selected = user_input.get(SELECTED_SENSORS, [])
            if not selected:
                errors["base"] = "no_sensors_selected"
            else:
                self._data[SELECTED_SENSORS] = selected
                return self.async_create_entry(title="", data=self._data)

        current_selected = list(get_selected_sensors(self._config_entry))
        return self.async_show_form(
            step_id="intervals",
            data_schema=_interval_schema(defaults=current_selected),
            errors=errors,
        )
