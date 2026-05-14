"""Base sensor class for Energy Scenarios."""

import logging
from datetime import timedelta
from decimal import Decimal

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util
from homeassistant.util.dt import now

from .const import DAILY, HOURLY, MANUAL, MONTHLY, QUARTERLY, WEEKLY, YEARLY

_LOGGER = logging.getLogger(__name__)


class BaseUtilitySensor(SensorEntity):
    """Base sensor for accumulating cost over a reset interval."""

    def __init__(self, hass: HomeAssistant, interval: str) -> None:
        super().__init__()
        self.hass = hass
        self._state = Decimal("0.00")
        self._unit_of_measurement = None
        self._interval = interval
        self.event_unsub: CALLBACK_TYPE | None = None
        self._last_update = now()
        self._last_reset = now()
        self._name = None

    def calculate_last_reset_time(self):
        """Return the most recent past interval boundary."""
        current_time = now()

        if self._interval == QUARTERLY:
            current_quarter = (current_time.minute // 15) * 15
            return current_time.replace(minute=current_quarter, second=0, microsecond=0)

        if self._interval == HOURLY:
            return current_time.replace(minute=0, second=0, microsecond=0)

        if self._interval == DAILY:
            return current_time.replace(hour=0, minute=0, second=0, microsecond=0)

        if self._interval == WEEKLY:
            days_since_monday = current_time.weekday()
            return (current_time - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        if self._interval == MONTHLY:
            return current_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        if self._interval == YEARLY:
            return current_time.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )

        return None

    def calculate_next_reset_time(self):
        """Return the datetime for the next interval boundary."""
        current_time = now()

        if self._interval == QUARTERLY:
            current_time = current_time.replace(second=0, microsecond=0)
            next_quarter = ((current_time.minute // 15) + 1) * 15
            if next_quarter >= 60:
                return current_time.replace(minute=0) + timedelta(hours=1)
            return current_time.replace(minute=next_quarter)

        if self._interval == HOURLY:
            return current_time.replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )

        if self._interval == DAILY:
            return current_time.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)

        if self._interval == WEEKLY:
            days_until_monday = (7 - current_time.weekday()) % 7
            next_monday = (current_time + timedelta(days=days_until_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if days_until_monday == 0:
                next_monday += timedelta(days=7)
            return next_monday

        if self._interval == MONTHLY:
            next_month = (current_time.replace(day=1) + timedelta(days=32)).replace(
                day=1
            )
            return next_month.replace(hour=0, minute=0, second=0, microsecond=0)

        if self._interval == YEARLY:
            return current_time.replace(
                year=current_time.year + 1,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

        return None

    def schedule_next_reset(self):
        """Schedule the next automatic reset."""
        if self._interval == MANUAL:
            return
        next_reset = self.calculate_next_reset_time()
        _LOGGER.debug("Scheduling next reset for %s at %s", self.name, next_reset)
        self.event_unsub = async_track_point_in_time(
            self.hass, self._async_reset_meter, next_reset
        )

    @callback
    def _async_reset_meter(self, *args) -> None:
        self.async_reset()
        self.schedule_next_reset()

    @callback
    def async_reset(self, *args):
        """Reset the accumulated cost to zero."""
        self._state = Decimal(0) if isinstance(self._state, Decimal) else 0

        for attr in ("_cumulative_cost",):
            if hasattr(self, attr):
                setattr(self, attr, 0)

        if hasattr(self, "_last_readings"):
            # Advance all last_readings to current values to avoid stale deltas after reset
            pass  # handled per-sensor in NetCostSensor.async_reset

        self._last_update = now()
        self._last_reset = now()
        self.async_write_ha_state()
        _LOGGER.debug("Meter reset for %s", self._name)

    @callback
    def async_calibrate(self, value):
        """Set the accumulated cost to a specific value."""
        _LOGGER.debug("Calibrate %s = %s", self._name, value)
        self._cumulative_cost = float(str(value))
        self._state = self._cumulative_cost
        self._last_update = now()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        if self.event_unsub:
            event_unsub = self.event_unsub
            self.event_unsub = None
            event_unsub()
        await super().async_will_remove_from_hass()

    @property
    def state(self):
        return self._state

    @property
    def device_class(self):
        return SensorDeviceClass.MONETARY

    @property
    def name(self):
        return self._name

    @property
    def icon(self):
        return "mdi:cash"

    @property
    def unit_of_measurement(self):
        return self._unit_of_measurement

    @property
    def last_reset(self):
        if self._interval == MANUAL:
            return None
        if isinstance(self._last_reset, str):
            return dt_util.parse_datetime(self._last_reset)
        return self._last_reset
