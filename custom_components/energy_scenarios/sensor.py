"""Sensor platform for Energy Scenarios."""

from decimal import Decimal, InvalidOperation
import logging
import math
from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util.dt import now

from .const import (
    BATTERY_CHARGE_SENSOR,
    BATTERY_DISCHARGE_SENSOR,
    DOMAIN,
    FEED_PRICE_SENSOR,
    GRID_EXPORT_SENSOR,
    GRID_IMPORT_SENSOR,
    INTERVALS,
    MANUAL,
    QUARTERLY,
    SELECTED_SENSORS,
    SERVICE_CALIBRATE,
    SERVICE_RESET_COST,
    SOLAR_PRODUCTION_SENSOR,
    TAKE_PRICE_SENSOR,
)
from . import get_entry_config, get_selected_sensors
from .entity import BaseUtilitySensor

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (adapted from dynamic_energy_cost)
# ---------------------------------------------------------------------------


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _state_to_float(state) -> float | None:
    if state is None or state.state in (None, "unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


_ENERGY_UNIT_TO_KWH = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
_PRICE_UNIT_TO_PER_KWH = {"wh": 1000.0, "kwh": 1.0, "mwh": 0.001}


def _energy_unit_factor(state) -> float:
    if state is None:
        return 1.0
    unit = state.attributes.get("unit_of_measurement", "kWh")
    return _ENERGY_UNIT_TO_KWH.get(unit, 1.0)


def _price_unit_factor(state) -> float:
    if state is None:
        return 1.0
    unit = state.attributes.get("unit_of_measurement", "")
    if "/" not in unit:
        return 1.0
    energy_part = unit.rsplit("/", 1)[-1].strip().lower()
    return _PRICE_UNIT_TO_PER_KWH.get(energy_part, 1.0)


def _last_reset_changed(old_state, new_state) -> bool:
    if old_state is None or new_state is None:
        return False
    old_lr = old_state.attributes.get("last_reset")
    new_lr = new_state.attributes.get("last_reset")
    return new_lr is not None and old_lr != new_lr


def _source_reset(current_state, last_known: float | None) -> bool:
    """Detect a reset on a total_increasing source sensor."""
    if current_state is None or last_known is None:
        return False
    if current_state.attributes.get("state_class") != "total_increasing":
        return False
    try:
        return float(current_state.state) < last_known
    except (TypeError, ValueError):
        return False


def validate_number(value):
    if _is_finite(value):
        return value
    raise vol.Invalid("Value is not a number")


# ---------------------------------------------------------------------------
# Sensor platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = get_entry_config(config_entry)
    selected = get_selected_sensors(config_entry)

    import_id = data[GRID_IMPORT_SENSOR]
    export_id = data.get(GRID_EXPORT_SENSOR)
    solar_id = data.get(SOLAR_PRODUCTION_SENSOR)
    charge_id = data.get(BATTERY_CHARGE_SENSOR)
    discharge_id = data.get(BATTERY_DISCHARGE_SENSOR)
    take_price_id = data[TAKE_PRICE_SENSOR]
    feed_price_id = data.get(FEED_PRICE_SENSOR)

    has_battery = bool(charge_id and discharge_id)
    has_solar = bool(solar_id)
    has_export = bool(export_id)

    name_prefix = data.get("name", "Unnamed")
    entry_id = config_entry.entry_id

    sensors: list[SensorEntity] = []

    # ------------------------------------------------------------------
    # Derived flow sensors (display)
    # ------------------------------------------------------------------

    # home_consumption (always useful when we have more than just import)
    if has_solar or has_battery or has_export:
        home_consumption_sources = [(import_id, 1.0)]
        if solar_id:
            home_consumption_sources.append((solar_id, 1.0))
        if discharge_id:
            home_consumption_sources.append((discharge_id, 1.0))
        if charge_id:
            home_consumption_sources.append((charge_id, -1.0))
        if export_id:
            home_consumption_sources.append((export_id, -1.0))

        sensors.append(
            DerivedFlowSensor(
                hass=hass,
                config_entry=config_entry,
                unique_id=f"{entry_id}_home_consumption",
                name=f"{name_prefix} Home Consumption",
                sources=home_consumption_sources,
            )
        )

    if has_battery:
        sensors.append(
            DerivedFlowSensor(
                hass=hass,
                config_entry=config_entry,
                unique_id=f"{entry_id}_cf_import_no_battery",
                name=f"{name_prefix} Counterfactual Import (No Battery)",
                sources=[(import_id, 1.0), (discharge_id, 1.0)],
            )
        )
        sensors.append(
            DerivedFlowSensor(
                hass=hass,
                config_entry=config_entry,
                unique_id=f"{entry_id}_cf_export_no_battery",
                name=f"{name_prefix} Counterfactual Export (No Battery)",
                sources=([(export_id, 1.0)] if export_id else []) + [(charge_id, 1.0)],
            )
        )

    # ------------------------------------------------------------------
    # NetCostSensors — one per (scenario × interval)
    # ------------------------------------------------------------------

    # Real scenario
    # take: grid_import; feed: grid_export
    real_take_sources = [(import_id, 1.0)]
    real_feed_sources = [(export_id, 1.0)] if export_id else []

    # No-battery scenario: take += discharge; feed += charge
    no_batt_take_sources = (
        [(import_id, 1.0), (discharge_id, 1.0)] if has_battery else None
    )
    no_batt_feed_sources = (
        (([(export_id, 1.0)] if export_id else []) + [(charge_id, 1.0)])
        if has_battery
        else None
    )

    # Baseline scenario: take = home_consumption (all 5 sensors), feed = none
    baseline_take_sources = None
    if has_solar or has_battery:
        baseline_take_sources = [(import_id, 1.0)]
        if solar_id:
            baseline_take_sources.append((solar_id, 1.0))
        if discharge_id:
            baseline_take_sources.append((discharge_id, 1.0))
        if charge_id:
            baseline_take_sources.append((charge_id, -1.0))
        if export_id:
            baseline_take_sources.append((export_id, -1.0))

    real_cost_ids: dict[str, str] = {}
    no_battery_cost_ids: dict[str, str] = {}
    baseline_cost_ids: dict[str, str] = {}

    for interval in INTERVALS:
        if interval not in selected:
            continue

        interval_label = _interval_label(interval)

        # Real
        real_uid = f"{entry_id}_real_{interval}_cost"
        real_cost_ids[interval] = real_uid
        sensors.append(
            NetCostSensor(
                hass=hass,
                config_entry=config_entry,
                interval=interval,
                unique_id=real_uid,
                name=f"{name_prefix} Real Cost {interval_label}",
                take_sources=real_take_sources,
                feed_sources=real_feed_sources,
                take_price_id=take_price_id,
                feed_price_id=feed_price_id,
            )
        )

        # No battery
        if has_battery:
            nb_uid = f"{entry_id}_no_battery_{interval}_cost"
            no_battery_cost_ids[interval] = nb_uid
            sensors.append(
                NetCostSensor(
                    hass=hass,
                    config_entry=config_entry,
                    interval=interval,
                    unique_id=nb_uid,
                    name=f"{name_prefix} No Battery Cost {interval_label}",
                    take_sources=no_batt_take_sources,
                    feed_sources=no_batt_feed_sources,
                    take_price_id=take_price_id,
                    feed_price_id=feed_price_id,
                )
            )

        # Baseline
        if baseline_take_sources is not None:
            bl_uid = f"{entry_id}_baseline_{interval}_cost"
            baseline_cost_ids[interval] = bl_uid
            sensors.append(
                NetCostSensor(
                    hass=hass,
                    config_entry=config_entry,
                    interval=interval,
                    unique_id=bl_uid,
                    name=f"{name_prefix} Baseline Cost {interval_label}",
                    take_sources=baseline_take_sources,
                    feed_sources=[],
                    take_price_id=take_price_id,
                    feed_price_id=None,
                )
            )

    # ------------------------------------------------------------------
    # SavingsSensors
    # ------------------------------------------------------------------
    for interval in INTERVALS:
        if interval not in selected:
            continue

        interval_label = _interval_label(interval)

        if interval in no_battery_cost_ids and interval in real_cost_ids:
            sensors.append(
                SavingsSensor(
                    hass=hass,
                    config_entry=config_entry,
                    interval=interval,
                    unique_id=f"{entry_id}_battery_savings_{interval}",
                    name=f"{name_prefix} Battery Savings {interval_label}",
                    counterfactual_id=no_battery_cost_ids[interval],
                    real_id=real_cost_ids[interval],
                )
            )

        if interval in baseline_cost_ids and interval in real_cost_ids:
            sensors.append(
                SavingsSensor(
                    hass=hass,
                    config_entry=config_entry,
                    interval=interval,
                    unique_id=f"{entry_id}_total_savings_{interval}",
                    name=f"{name_prefix} Total Savings {interval_label}",
                    counterfactual_id=baseline_cost_ids[interval],
                    real_id=real_cost_ids[interval],
                )
            )

        if interval in baseline_cost_ids and interval in no_battery_cost_ids:
            sensors.append(
                SavingsSensor(
                    hass=hass,
                    config_entry=config_entry,
                    interval=interval,
                    unique_id=f"{entry_id}_solar_savings_{interval}",
                    name=f"{name_prefix} Solar Savings {interval_label}",
                    counterfactual_id=baseline_cost_ids[interval],
                    real_id=no_battery_cost_ids[interval],
                )
            )

    async_add_entities(sensors)

    # Register services
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(SERVICE_RESET_COST, {}, "async_reset")
    platform.async_register_entity_service(
        SERVICE_CALIBRATE,
        {vol.Required("value"): validate_number},
        "async_calibrate",
    )


def _interval_label(interval: str) -> str:
    if interval == QUARTERLY:
        return "15-Minute"
    return interval.replace("_", " ").title()


def _device_info(config_entry: ConfigEntry) -> dict:
    """Return device info for the helper device shared by all sensors in this entry."""
    name = config_entry.data.get("name") or config_entry.options.get("name") or "Unnamed"
    return {
        "identifiers": {(DOMAIN, config_entry.entry_id)},
        "name": f"{name} Energy Scenarios",
        "manufacturer": "Custom Integration",
    }


# ---------------------------------------------------------------------------
# DerivedFlowSensor
# ---------------------------------------------------------------------------


class DerivedFlowSensor(SensorEntity, RestoreEntity):
    """Display-only cumulative energy sensor derived from weighted source sensors.

    Represents counterfactual or derived energy flows (e.g. home consumption,
    counterfactual grid import without battery). Not used in cost computation —
    purely for dashboards.
    """

    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        unique_id: str,
        name: str,
        sources: list[tuple[str, float]],
    ) -> None:
        self.hass = hass
        self._config_entry = config_entry
        self._attr_unique_id = unique_id
        self._name = name
        self._sources = sources  # list of (entity_id, weight)
        self._state: float = 0.0
        self._last_readings: dict[str, float | None] = {eid: None for eid, _ in sources}
        self._energy_factors: dict[str, float] = {eid: 1.0 for eid, _ in sources}
        self._unsubs: list = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def device_info(self) -> dict:
        return _device_info(self._config_entry)

    @property
    def state(self):
        return round(self._state, 4)

    @property
    def unit_of_measurement(self) -> str:
        return "kWh"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"last_readings": {k: v for k, v in self._last_readings.items()}}

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                self._state = float(last.state)
            except (TypeError, ValueError):
                pass
            if last.attributes.get("last_readings"):
                for eid, val in last.attributes["last_readings"].items():
                    if eid in self._last_readings and val is not None:
                        self._last_readings[eid] = float(val)

        # Resolve unit factors from current sensor states
        for eid, _ in self._sources:
            state = self.hass.states.get(eid)
            if state:
                self._energy_factors[eid] = _energy_unit_factor(state)
                if self._last_readings[eid] is None:
                    val = _state_to_float(state)
                    if val is not None:
                        self._last_readings[eid] = val

        for eid, _ in self._sources:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [eid], self._handle_source_update
                )
            )

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _handle_source_update(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        current_val = _state_to_float(new_state)

        if current_val is None:
            return

        # Resolve unit factor if not yet known
        if self._energy_factors.get(entity_id, 1.0) == 1.0:
            self._energy_factors[entity_id] = _energy_unit_factor(new_state)

        last = self._last_readings.get(entity_id)
        if last is None:
            self._last_readings[entity_id] = current_val
            self.async_write_ha_state()
            return

        # Detect reset
        reset = (
            current_val == 0
            or _last_reset_changed(old_state, new_state)
            or _source_reset(new_state, last)
        )
        if reset:
            _LOGGER.debug(
                "%s: source %s reset, reinitialising baseline", self._name, entity_id
            )
            self._last_readings[entity_id] = current_val
            self.async_write_ha_state()
            return

        delta_kwh = (current_val - last) * self._energy_factors[entity_id]
        weight = next(w for eid, w in self._sources if eid == entity_id)
        self._state += delta_kwh * weight
        self._last_readings[entity_id] = current_val
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# NetCostSensor
# ---------------------------------------------------------------------------


class NetCostSensor(BaseUtilitySensor, RestoreEntity):
    """Accumulates net energy cost for one scenario over a reset interval.

    Subscribes directly to raw source sensors (not to DerivedFlowSensor) so
    that out-of-sync sensor updates are handled correctly: since cost is a
    linear function of energy, each sensor's contribution is processed
    independently when it arrives — order doesn't affect the final total.
    """

    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        interval: str,
        unique_id: str,
        name: str,
        take_sources: list[tuple[str, float]],
        feed_sources: list[tuple[str, float]],
        take_price_id: str,
        feed_price_id: str | None,
    ) -> None:
        super().__init__(hass, interval)
        self._config_entry = config_entry
        self._attr_unique_id = unique_id
        self._name = name

        self._take_sources = take_sources  # (entity_id, sign)
        self._feed_sources = feed_sources  # (entity_id, sign)
        self._take_price_id = take_price_id
        self._feed_price_id = feed_price_id

        # Per-source last-known readings (to compute deltas)
        all_source_ids = {eid for eid, _ in take_sources + feed_sources}
        self._last_readings: dict[str, float | None] = {
            eid: None for eid in all_source_ids
        }
        self._energy_factors: dict[str, float] = {eid: 1.0 for eid in all_source_ids}

        self._cumulative_cost: float = 0.0
        self._unsubs: list = []

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def device_info(self) -> dict:
        return _device_info(self._config_entry)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "cumulative_cost": self._cumulative_cost,
            "last_readings": {k: v for k, v in self._last_readings.items()},
            "interval": self._interval,
        }

    async def async_added_to_hass(self) -> None:
        # Restore state
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                self._state = Decimal(str(last.state))
                self._cumulative_cost = float(last.state)
            except (InvalidOperation, TypeError, ValueError):
                pass
            attrs = last.attributes
            if attrs.get("cumulative_cost") is not None:
                self._cumulative_cost = float(attrs["cumulative_cost"])
                self._state = Decimal(str(self._cumulative_cost))
            if attrs.get("last_readings"):
                for eid, val in attrs["last_readings"].items():
                    if eid in self._last_readings and val is not None:
                        self._last_readings[eid] = float(val)
            if attrs.get("last_reset"):
                self._last_reset = attrs["last_reset"]

        # Resolve unit factors and initialise baselines from current states
        for eid in self._last_readings:
            state = self.hass.states.get(eid)
            if state:
                self._energy_factors[eid] = _energy_unit_factor(state)
                if self._last_readings[eid] is None:
                    val = _state_to_float(state)
                    if val is not None:
                        self._last_readings[eid] = val

        # Resolve currency from price sensor
        price_state = self.hass.states.get(self._take_price_id)
        if price_state:
            unit = price_state.attributes.get("unit_of_measurement", "")
            if "/" in unit:
                self._unit_of_measurement = unit.split("/")[0].strip()

        # Subscribe to all energy source sensors
        all_source_ids = list(
            {eid for eid, _ in self._take_sources + self._feed_sources}
        )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, all_source_ids, self._handle_energy_update
            )
        )

        # Subscribe to price sensors
        price_ids = [self._take_price_id]
        if self._feed_price_id:
            price_ids.append(self._feed_price_id)
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, price_ids, self._handle_price_update
            )
        )

        self.schedule_next_reset()

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_energy_update(self, event: Event) -> None:
        """Process a delta from one energy source sensor."""
        entity_id = event.data["entity_id"]
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        current_val = _state_to_float(new_state)

        if current_val is None:
            return

        if self._energy_factors.get(entity_id, 1.0) == 1.0:
            self._energy_factors[entity_id] = _energy_unit_factor(new_state)

        last = self._last_readings.get(entity_id)
        if last is None:
            self._last_readings[entity_id] = current_val
            return

        reset = (
            current_val == 0
            or _last_reset_changed(old_state, new_state)
            or _source_reset(new_state, last)
        )
        if reset:
            _LOGGER.debug(
                "%s: source %s reset, reinitialising baseline", self._name, entity_id
            )
            self._last_readings[entity_id] = current_val
            return

        delta_kwh = (current_val - last) * self._energy_factors[entity_id]
        self._last_readings[entity_id] = current_val

        take_price = self._current_take_price()
        feed_price = self._current_feed_price()

        for src_id, sign in self._take_sources:
            if src_id == entity_id and take_price is not None:
                self._cumulative_cost += delta_kwh * sign * take_price

        for src_id, sign in self._feed_sources:
            if src_id == entity_id and feed_price is not None:
                self._cumulative_cost -= delta_kwh * sign * feed_price

        self._state = Decimal(str(self._cumulative_cost))
        self.async_write_ha_state()

    @callback
    def _handle_price_update(self, event: Event) -> None:
        """Finalise pending energy at the old price before the new price takes effect."""
        entity_id = event.data["entity_id"]
        old_price_state = event.data.get("old_state")
        old_price = _state_to_float(old_price_state)

        if old_price is None:
            return

        old_price_factor = _price_unit_factor(old_price_state)

        if entity_id == self._take_price_id:
            # Finalise all take-contributing sensors at old take price
            for src_id, sign in self._take_sources:
                self._finalise_source_at_price(
                    src_id, sign, old_price * old_price_factor, is_feed=False
                )

        elif entity_id == self._feed_price_id:
            # Finalise all feed-contributing sensors at old feed price
            for src_id, sign in self._feed_sources:
                self._finalise_source_at_price(
                    src_id, sign, old_price * old_price_factor, is_feed=True
                )

        self._state = Decimal(str(self._cumulative_cost))
        self.async_write_ha_state()

    def _finalise_source_at_price(
        self, entity_id: str, sign: float, price: float, is_feed: bool
    ) -> None:
        """Apply pending delta for one source at the given price and advance its baseline."""
        current_state = self.hass.states.get(entity_id)
        current_val = _state_to_float(current_state)
        last = self._last_readings.get(entity_id)

        if current_val is None or last is None:
            return

        if _source_reset(current_state, last):
            self._last_readings[entity_id] = current_val
            return

        delta_kwh = (current_val - last) * self._energy_factors.get(entity_id, 1.0)
        self._last_readings[entity_id] = current_val

        if is_feed:
            self._cumulative_cost -= delta_kwh * sign * price
        else:
            self._cumulative_cost += delta_kwh * sign * price

    def _current_take_price(self) -> float | None:
        state = self.hass.states.get(self._take_price_id)
        val = _state_to_float(state)
        if val is None:
            return None
        return val * _price_unit_factor(state)

    def _current_feed_price(self) -> float | None:
        if not self._feed_price_id:
            return None
        state = self.hass.states.get(self._feed_price_id)
        val = _state_to_float(state)
        if val is None:
            return None
        return val * _price_unit_factor(state)

    @callback
    def async_reset(self, *args):
        """Reset cost; advance all source baselines to current values."""
        for eid in list(self._last_readings):
            state = self.hass.states.get(eid)
            val = _state_to_float(state)
            if val is not None:
                self._last_readings[eid] = val

        self._cumulative_cost = 0.0
        self._state = Decimal("0.00")
        self._last_reset = now()
        self.async_write_ha_state()
        _LOGGER.debug("Meter reset for %s", self._name)

    @callback
    def async_calibrate(self, value):
        self._cumulative_cost = float(str(value))
        self._state = Decimal(str(self._cumulative_cost))
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# SavingsSensor
# ---------------------------------------------------------------------------


class SavingsSensor(SensorEntity, RestoreEntity):
    """Shows the cost difference between a counterfactual and real scenario.

    No independent accumulation — simply reads the current state of two
    NetCostSensors. Resets are handled by the underlying sensors.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_icon = "mdi:piggy-bank"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        interval: str,
        unique_id: str,
        name: str,
        counterfactual_id: str,
        real_id: str,
    ) -> None:
        self.hass = hass
        self._config_entry = config_entry
        self._interval = interval
        self._attr_unique_id = unique_id
        self._name = name
        self._counterfactual_id = counterfactual_id
        self._real_id = real_id
        self._state: float | None = None
        self._unit_of_measurement: str | None = None
        self._unsubs: list = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def device_info(self) -> dict:
        return _device_info(self._config_entry)

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self) -> str | None:
        return self._unit_of_measurement

    @property
    def state_class(self):
        return SensorStateClass.TOTAL

    @property
    def last_reset(self):
        """Mirror the last_reset of the counterfactual cost sensor."""
        state = self.hass.states.get(self._counterfactual_id)
        if state:
            lr = state.attributes.get("last_reset")
            if lr:
                from homeassistant.util import dt as dt_util

                if isinstance(lr, str):
                    return dt_util.parse_datetime(lr)
                return lr
        return None

    async def async_added_to_hass(self) -> None:
        # Resolve currency unit from the real cost sensor
        real_state = self.hass.states.get(self._real_id)
        if real_state:
            self._unit_of_measurement = real_state.attributes.get("unit_of_measurement")

        self._recompute()

        self._unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self._counterfactual_id, self._real_id],
                self._handle_update,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _handle_update(self, event: Event) -> None:
        # Pick up unit if not yet resolved
        if self._unit_of_measurement is None:
            real_state = self.hass.states.get(self._real_id)
            if real_state:
                self._unit_of_measurement = real_state.attributes.get(
                    "unit_of_measurement"
                )

        self._recompute()
        self.async_write_ha_state()

    def _recompute(self) -> None:
        cf_state = self.hass.states.get(self._counterfactual_id)
        real_state = self.hass.states.get(self._real_id)
        cf_val = _state_to_float(cf_state)
        real_val = _state_to_float(real_state)

        if cf_val is not None and real_val is not None:
            self._state = round(cf_val - real_val, 4)
        else:
            self._state = None
